package com.pekaflow.agent;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStreamReader;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

/**
 * 增量发布的撤销：把发布前备份的文件盖回去，并删掉那次新增的文件。
 *
 * 回滚不重新构建，只动这台机器上已经躺着的备份——老代码未必还编译得出来，
 * 依赖源和 SDK 版本都在变，而故障时最缺的就是时间。
 *
 * 备份目录是 file-transfer 发布时写的，里面有两份清单：
 *   _backup_files.txt  发布时被覆盖的文件（备份目录里存着它们的原件）
 *   _added_files.txt   发布时新增的文件（备份目录里没有原件，回滚要删掉）
 * 少了删除这一步，回滚后站点里会留着新版本才有的 DLL，
 * 老代码一旦按名字加载到它，故障现象会比回滚前更难查。
 */
public class RollbackFiles {

    private final ApiClient api;
    private final int agentId;
    private final List<String> allowPaths;
    /** 安装时备份根；回滚也只能读这棵树，防止把别人的备份或 /tmp 当还原源。 */
    private final String configuredBackupRoot;

    public RollbackFiles(ApiClient api, int agentId, List<String> allowPaths) {
        this(api, agentId, allowPaths, "");
    }

    public RollbackFiles(ApiClient api, int agentId, List<String> allowPaths, String configuredBackupRoot) {
        this.api = api;
        this.agentId = agentId;
        this.allowPaths = allowPaths;
        this.configuredBackupRoot = configuredBackupRoot == null ? "" : configuredBackupRoot.trim();
    }

    public boolean run(Map<String, Object> with, int taskId, LogSink sink) throws Exception {
        File target;
        try {
            target = NodeExecutor.resolveAllowed(allowPaths, Json.str(with.get("targetDir")));
        } catch (IllegalArgumentException e) {
            sink.log("  " + e.getMessage());
            return false;
        }
        if (!target.isDirectory()) {
            sink.log("  站点根目录不存在：" + target.getPath());
            return false;
        }

        String rawBackup = Json.str(with.get("backupDir"));
        if (rawBackup == null || rawBackup.trim().isEmpty()) {
            sink.log("  没有备份目录，无法回滚。该次发布可能是在支持回滚之前发的");
            return false;
        }
        File backupDir;
        try {
            // 备份目录是「读取源」，里面的文件会被原样复制进站点。不校验的话，
            // 一条被篡改的回滚指令就能把 /etc 下的文件搬进可被 HTTP 下载的目录。
            // 用和发布时同一套校验，两边对称，不会出现「发的时候不让写、回滚却能读」
            backupDir = NodeExecutor.resolveBackupRoot(allowPaths, rawBackup.trim());
            NodeExecutor.assertInsideConfiguredBackupRoot(backupDir, configuredBackupRoot);
        } catch (IllegalArgumentException e) {
            sink.log("  备份目录不可用：" + e.getMessage());
            return false;
        }
        if (!backupDir.isDirectory()) {
            sink.log("  备份目录不存在：" + backupDir.getPath());
            sink.log("  可能已被清理（保留份数上限）或磁盘被整理过，只能用旧版本重新发一次");
            return false;
        }

        List<String> toRestore = readLines(new File(backupDir, "_backup_files.txt"));
        List<String> toDelete = readLines(new File(backupDir, "_added_files.txt"));
        if (toRestore.isEmpty() && toDelete.isEmpty()) {
            sink.log("  备份目录里没有文件清单，无法确定要还原什么：" + backupDir.getPath());
            return false;
        }

        sink.log("  备份目录：" + backupDir.getPath());
        sink.log("  将还原 " + toRestore.size() + " 个文件，删除 " + toDelete.size() + " 个新增文件");

        // 先确认备份文件都在，再动生产目录：还原到一半发现某个文件丢了，
        // 站点会停在新旧混合的状态，比回滚前更糟
        List<String> missing = new ArrayList<String>();
        for (String name : toRestore) {
            if (!NodeExecutor.safeChild(backupDir, name).isFile()) {
                missing.add(name);
            }
        }
        if (!missing.isEmpty()) {
            sink.log("  备份不完整，缺少 " + missing.size() + " 个文件，已中止（生产文件未被改动）：");
            preview(sink, missing);
            return false;
        }

        if (Json.bool(with.get("dryRun"))) {
            sink.log("  预演模式：不还原、不删除，生产文件未被改动");
            return true;
        }

        int restored = 0;
        for (String name : toRestore) {
            File src = NodeExecutor.safeChild(backupDir, name);
            File dst = NodeExecutor.safeChild(target, name);
            FileTransfer.copyReplace(src, dst);
            restored++;
        }
        sink.log("  已还原 " + restored + " 个文件");

        int deleted = 0;
        List<String> failed = new ArrayList<String>();
        for (String name : toDelete) {
            File f = NodeExecutor.safeChild(target, name);
            if (!f.exists()) {
                continue;
            }
            if (f.delete()) {
                deleted++;
            } else {
                failed.add(name);
            }
        }
        sink.log("  已删除 " + deleted + " 个新增文件");
        if (!failed.isEmpty()) {
            // 删不掉通常是被 w3wp 占着，说明应用池没停干净。文件还在会导致
            // 回滚不彻底，必须报失败让人知道，不能悄悄放过
            sink.log("  有 " + failed.size() + " 个文件删不掉（多半被进程占用，应用池没停干净）：");
            preview(sink, failed);
            return false;
        }

        cleanupEmptyDirs(target, toDelete, sink);

        // 销账：同一份备份只能撤销一次，再撤一次会把更早的版本盖上来
        int recordId = Json.integer(with.get("_deployment_record_id"));
        if (recordId > 0 && agentId > 0 && taskId > 0) {
            try {
                api.post("/api/v1/agents/" + agentId + "/tasks/" + taskId
                        + "/deployment/" + recordId + "/undone", new java.util.HashMap<String, Object>());
            } catch (Exception e) {
                sink.log("  提示：回滚已完成，但销账失败（" + e.getMessage() + "）");
            }
        }
        return true;
    }

    /** 删完新增文件后，本次新建的空目录也一并清掉，别在站点里留下一堆空壳。 */
    private static void cleanupEmptyDirs(File target, List<String> deleted, LogSink sink) {
        List<String> dirs = new ArrayList<String>();
        for (String name : deleted) {
            String n = name.replace('\\', '/');
            int idx = n.lastIndexOf('/');
            if (idx > 0) {
                dirs.add(n.substring(0, idx));
            }
        }
        // 深的先删，删完子目录父目录才可能变空
        Collections.sort(dirs);
        Collections.reverse(dirs);
        for (String d : dirs) {
            try {
                File dir = NodeExecutor.safeChild(target, d);
                String[] children = dir.list();
                if (dir.isDirectory() && children != null && children.length == 0) {
                    dir.delete();
                }
            } catch (Exception ignored) {
                // 清空目录是锦上添花，失败不该让回滚变成失败
            }
        }
    }

    private static void preview(LogSink sink, List<String> files) {
        int limit = Math.min(files.size(), 20);
        for (int i = 0; i < limit; i++) {
            sink.log("    " + files.get(i));
        }
        if (files.size() > limit) {
            sink.log("    ... 另有 " + (files.size() - limit) + " 个");
        }
    }

    private static List<String> readLines(File file) throws Exception {
        List<String> out = new ArrayList<String>();
        if (!file.isFile()) {
            return out;
        }
        BufferedReader reader = new BufferedReader(
                new InputStreamReader(new FileInputStream(file), "UTF-8"));
        try {
            String line;
            while ((line = reader.readLine()) != null) {
                String s = line.trim();
                if (!s.isEmpty()) {
                    out.add(s);
                }
            }
        } finally {
            reader.close();
        }
        return out;
    }
}
