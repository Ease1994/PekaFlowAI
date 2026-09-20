package com.pekaflow.agent;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.Enumeration;
import java.util.List;
import java.util.Map;
import java.util.function.BooleanSupplier;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

/**
 * 发送文件到节点：拉包 → 备份将被覆盖的文件 → 解压覆盖。
 *
 * 包由节点自己从平台拉，平台不往生产机推任何东西，生产机也不需要开入站端口。
 *
 * 备份只备本次会动到的文件，因为增量发布的回滚只需要把这些文件换回去；
 * 同时单独记下「本次新增」的文件清单，回滚时要删掉它们，否则会留下一堆残留。
 */
public class FileTransfer {

    private final ApiClient api;
    private final int agentId;
    private final List<String> allowPaths;
    private final File agentHome;
    /** 安装时定下的备份根；步骤里 backupDir 留空时用它。 */
    private final String configuredBackupRoot;

    // 本次备份落在哪、动了多少文件。执行完由 NodeExecutor 取走上报给平台，
    // 回滚时平台才知道该让哪台机器还原哪个目录
    private String backupDir = "";
    private String summary = "";
    /** 规范化后的备份根。清理时用来核对父目录必须是这棵树下的「项目/流水线」这一层。 */
    private File backupRoot;

    public FileTransfer(ApiClient api, int agentId, List<String> allowPaths, File agentHome) {
        this(api, agentId, allowPaths, agentHome, null);
    }

    public FileTransfer(ApiClient api, int agentId, List<String> allowPaths, File agentHome,
                        String configuredBackupRoot) {
        this.api = api;
        this.agentId = agentId;
        this.allowPaths = allowPaths;
        this.agentHome = agentHome;
        this.configuredBackupRoot = configuredBackupRoot == null ? "" : configuredBackupRoot.trim();
    }

    public String getBackupDir() {
        return backupDir;
    }

    public String getSummary() {
        return summary;
    }

    public boolean run(Map<String, Object> with, Map<String, Object> vars, int taskId, LogSink sink)
            throws Exception {
        return run(with, vars, taskId, sink, null);
    }

    /**
     * 拉包、备份、解压覆盖。
     *
     * @param cancelled 解压循环中可中断；取消后已写入的文件靠备份回滚，由调用方补偿启服
     */
    public boolean run(Map<String, Object> with, Map<String, Object> vars, int taskId, LogSink sink,
                       BooleanSupplier cancelled) throws Exception {
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
        boolean dryRun = Json.bool(with.get("dryRun"));
        // 整步总超时：大包、慢盘、卡住的网络都不能把节点线程占死。
        // 节点并发是 1，这一步不结束，后面的启服也发不出去。
        long deadlineMs = resolveDeadline(with);

        // 1. 拉包
        File work = new File(agentHome, "packages");
        File zipFile = new File(work, "task-" + taskId + ".zip");
        purgeStalePackages(work, zipFile);
        sink.log("  从平台拉取增量包 ...");
        try {
            api.downloadToFile(
                    "/api/v1/agents/" + agentId + "/tasks/" + taskId + "/deploy-package",
                    zipFile, deadlineMs);
        } catch (Exception e) {
            sink.log("  拉取增量包失败：" + e.getMessage());
            sink.log("  请检查本次发布前是否执行了「提取增量发布包」步骤");
            return false;
        }
        if (stopped(cancelled, deadlineMs, sink, "拉取增量包")) {
            return false;
        }
        sink.log("  已拉取：" + (zipFile.length() / 1024) + " KB");

        ZipFile zf = null;
        try {
            zf = new ZipFile(zipFile);
            List<String> entries;
            try {
                entries = listEntries(zf);
            } catch (IllegalStateException e) {
                sink.log("  " + e.getMessage());
                return false;
            }
            if (entries.isEmpty()) {
                sink.log("  增量包是空的，没有可发布的文件");
                return false;
            }

            // 2. 分类：哪些是覆盖已有文件，哪些是新增
            List<String> overwrite = new ArrayList<String>();
            List<String> added = new ArrayList<String>();
            long backupBytes = 0;
            for (String name : entries) {
                File dest = NodeExecutor.safeChild(target, name);
                if (dest.isFile()) {
                    overwrite.add(name);
                    backupBytes += dest.length();
                } else {
                    added.add(name);
                }
            }
            sink.log("  本次共 " + entries.size() + " 个文件：覆盖 " + overwrite.size()
                    + " 个，新增 " + added.size() + " 个");
            preview(sink, "覆盖", overwrite);
            preview(sink, "新增", added);

            if (dryRun) {
                sink.log("  预演模式：不备份、不解压，生产文件未被改动");
                return true;
            }

            if (!enoughSpace(target, estimateUnpacked(zf, entries, zipFile.length()),
                    "站点目录所在磁盘", sink)) {
                return false;
            }

            // 3. 备份：只备会被覆盖的，外加一份新增清单供回滚删除
            File backupDir;
            try {
                backupDir = prepareBackupDir(with, vars, taskId);
            } catch (IllegalArgumentException e) {
                sink.log("  " + e.getMessage());
                return false;
            }
            if (!enoughSpace(backupDir, backupBytes + (64L * 1024 * 1024),
                    "备份目录所在磁盘", sink)) {
                return false;
            }
            sink.log("  备份目录：" + backupDir.getPath());
            for (String name : overwrite) {
                if (stopped(cancelled, deadlineMs, sink, "备份")) {
                    return false;
                }
                File src = NodeExecutor.safeChild(target, name);
                File dst = NodeExecutor.safeChild(backupDir, name);
                mkdirs(dst.getParentFile());
                copy(src, dst, deadlineMs);
            }
            writeLines(new File(backupDir, "_added_files.txt"), added);
            writeLines(new File(backupDir, "_backup_files.txt"), overwrite);
            sink.log("  已备份 " + overwrite.size() + " 个将被覆盖的文件");
            this.backupDir = backupDir.getCanonicalPath();
            this.summary = "覆盖 " + overwrite.size() + " 个文件，新增 " + added.size() + " 个";

            // 4. 解压覆盖。边写边累计真实体积，声明大小可以造假。
            int written = 0;
            long unpacked = 0;
            for (String name : entries) {
                if (stopped(cancelled, deadlineMs, sink, "解压覆盖")) {
                    if (written > 0) {
                        sink.log("  已写入 " + written + " 个文件，可用本次备份回滚");
                    }
                    return false;
                }
                File dest = NodeExecutor.safeChild(target, name);
                mkdirs(dest.getParentFile());
                long n = extract(zf, name, dest, deadlineMs);
                unpacked += n;
                if (unpacked > MAX_TOTAL_UNPACKED) {
                    throw new IllegalStateException(
                            "解压累计超过 " + (MAX_TOTAL_UNPACKED / 1024 / 1024 / 1024)
                                    + "GB，疑似解压炸弹，已中止");
                }
                written++;
            }
            sink.log("  已写入 " + written + " 个文件到 " + target.getPath());

            int keep = resolveKeepBackups(with.get("keepBackups"));
            int requested = Json.integer(with.get("keepBackups"));
            if (requested > MAX_KEEP_BACKUPS) {
                sink.log("  备份保留份数 " + requested + " 超过上限 " + MAX_KEEP_BACKUPS + "，按上限处理");
            }
            // 发布已经成功。清备份失败只是占盘；删错才是事故，所以清理内部不确定就跳过。
            cleanupOldBackups(backupDir, this.backupRoot, target, keep, sink);
            return true;
        } finally {
            if (zf != null) {
                try {
                    zf.close();
                } catch (Exception ignored) {
                    // ignore
                }
            }
            zipFile.delete();
        }
    }

    private static void preview(LogSink sink, String label, List<String> files) {
        int limit = Math.min(files.size(), 20);
        for (int i = 0; i < limit; i++) {
            sink.log("    [" + label + "] " + files.get(i));
        }
        if (files.size() > limit) {
            sink.log("    [" + label + "] ... 另有 " + (files.size() - limit) + " 个");
        }
    }

    /**
     * 备份目录：{根}/{项目}/{流水线}/{日期时间}/。
     *
     * 根默认是安装节点时的 --backup-root。步骤里只有打开「自定义备份目录」才填 backupDir：
     * 相对路径接到这个根下面，绝对路径也必须仍在根内。换盘属于节点安装，不在流水线里改。
     *
     * 按项目和流水线分层，是为了让人在生产机上直接翻目录就能找到某条线的历史版本，
     * 不用回平台查任务号；带上时分秒是因为同一天往往要发好几次，只按日期会互相覆盖。
     */
    private File prepareBackupDir(Map<String, Object> with, Map<String, Object> vars, int taskId)
            throws Exception {
        String raw = Json.str(with.get("backupDir"));
        File root;
        if (raw != null && !raw.trim().isEmpty()) {
            root = NodeExecutor.resolvePipelineBackupRoot(allowPaths, raw.trim(), configuredBackupRoot);
        } else if (!configuredBackupRoot.isEmpty()) {
            root = NodeExecutor.resolveBackupRoot(allowPaths, configuredBackupRoot);
            NodeExecutor.assertInsideConfiguredBackupRoot(root, configuredBackupRoot);
        } else {
            // 默认根也走同一套校验：不能是盘符根、不能写进站点里面
            root = NodeExecutor.resolveBackupRoot(allowPaths, defaultBackupRoot().getPath());
            NodeExecutor.assertInsideConfiguredBackupRoot(root, configuredBackupRoot);
        }
        this.backupRoot = root;

        String project = folderName(Json.str(vars.get("BK_CI_PROJECT_NAME")), "unknown-project");
        String pipeline = folderName(Json.str(vars.get("BK_CI_PIPELINE_NAME")), "unknown-pipeline");
        String stamp = new SimpleDateFormat("yyyyMMdd-HHmmss").format(new Date());
        if (!isBackupStampName(stamp)) {
            throw new IllegalStateException("内部错误：备份目录名不是日期时间格式 " + stamp);
        }

        File dir = new File(new File(new File(root, project), pipeline), stamp);
        mkdirs(dir, "这不是允许目录拦住的，是运行账号写不进去。"
                + "默认用安装时建好的备份根；自定义相对路径要先由运行账号写得进去");
        dir = dir.getCanonicalFile();
        assertStampDirUnderRoot(dir, root, project, pipeline, stamp);
        return dir;
    }

    /**
     * 刚建好的备份目录必须是 {根}/{项目}/{流水线}/{日期时间}。
     * 少一层或多一层都不行：后面按「父目录里的子文件夹」清理时，
     * 会把别的项目、甚至盘上其它目录当成旧备份删掉。
     */
    static void assertStampDirUnderRoot(File stampDir, File root, String project, String pipeline, String stamp)
            throws Exception {
        File canonStamp = stampDir.getCanonicalFile();
        File canonRoot = root.getCanonicalFile();
        File expected = new File(new File(new File(canonRoot, project), pipeline), stamp).getCanonicalFile();
        if (!canonStamp.equals(expected)) {
            throw new IllegalStateException(
                    "备份目录越界或层级不对：实际 " + canonStamp.getPath()
                            + "，期望 " + expected.getPath());
        }
        File parent = canonStamp.getParentFile();
        if (parent == null || parent.getParentFile() == null || parent.getParentFile().getParentFile() == null) {
            throw new IllegalStateException("备份目录层级不足，拒绝在过浅的路径上落盘：" + canonStamp.getPath());
        }
    }

    /**
     * 安装时没配 --backup-root 的老 Agent 才走到这里。
     *
     * 生产机系统盘通常只留系统自己的量。备份默默堆上去会把盘写满，
     * IIS 连日志都写不出，比这次发布失败严重得多。
     * 没有 D:/E: 时拒绝默认到系统盘，必须在安装节点时设置 BACKUP_ROOT。
     */
    private File defaultBackupRoot() {
        if (File.separatorChar == '\\') {
            for (String drive : new String[] {"D:", "E:"}) {
                File d = new File(drive + File.separator);
                if (d.isDirectory()) {
                    return new File(d, "backup");
                }
            }
            throw new IllegalArgumentException(
                    "本机没有独立数据盘，不能把备份默认落到系统盘。"
                            + "请在安装节点时设置 BACKUP_ROOT，指向站点盘或数据盘上的目录（不要放进站点里面）");
        }
        return new File("/var/release/backup");
    }

    /** 项目名/流水线名要当目录名用。`.` / `..` 会让父目录爬出备份根，一律改成 fallback。 */
    static String folderName(String raw, String fallback) {
        if (raw == null) {
            return fallback;
        }
        String s = raw.trim();
        if (s.isEmpty() || s.equals(".") || s.equals("..")) {
            return fallback;
        }
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c < 0x20 || "\\/:*?\"<>|".indexOf(c) >= 0) {
                sb.append('_');
            } else {
                sb.append(c);
            }
        }
        // Windows 会把结尾的点和空格吃掉，留着会导致创建出来的目录名和预期对不上
        String out = sb.toString().trim();
        while (out.endsWith(".")) {
            out = out.substring(0, out.length() - 1).trim();
        }
        if (out.isEmpty() || out.equals(".") || out.equals("..")) {
            return fallback;
        }
        return out;
    }

    /** 与插件 schema、平台注入的默认值一致。YAML 没写时不能当成 0（0 会跳过清理）。 */
    private static final int DEFAULT_KEEP_BACKUPS = 20;

    /** 单条流水线在生产机上允许保留的备份份数上限，防止填 99999 把盘写满。 */
    static final int MAX_KEEP_BACKUPS = 50;

    /**
     * 单次发布最多删掉的旧备份份数。
     * 防止目录判错或时钟回拨时一次把历史全清掉；多出来的下次发布再收。
     * 大站备份堆积时 5 份/次收得太慢，盘会先满；20 仍远小于一次清空。
     */
    static final int MAX_CLEANUP_PER_RUN = 20;

    /**
     * 项目名/流水线名缺失、落到 unknown-* 时的保留上限。
     * 多条线会挤在同一层，不能按流水线的 20 份留，否则盘只会涨。
     */
    static final int FALLBACK_MAX_KEEP = 5;

    /** 读写文件用的缓冲区。8KB 系统调用太碎，解压时会多占 CPU。 */
    private static final int IO_BUFFER_BYTES = 64 * 1024;

    /** 发送文件整步默认超时：1 小时。 */
    static final long DEFAULT_STEP_TIMEOUT_MS = 60L * 60L * 1000L;

    /** 发送文件整步超时上限：2 小时。流水线填再大也按这个封顶。 */
    static final long MAX_STEP_TIMEOUT_MS = 2L * 60L * 60L * 1000L;

    /** 本插件写入备份目录的标记文件。没有它的日期文件夹一律不动。 */
    static final String BACKUP_MARKER = "_backup_files.txt";
    static final String ADDED_MARKER = "_added_files.txt";

    /**
     * 备份保留份数：缺省 20；显式 0 表示只留本次（按 1 处理）；超过上限按上限。
     */
    private static int resolveKeepBackups(Object raw) {
        if (raw == null) {
            return DEFAULT_KEEP_BACKUPS;
        }
        String text = String.valueOf(raw).trim();
        if (text.isEmpty()) {
            return DEFAULT_KEEP_BACKUPS;
        }
        int keep = Json.integer(raw);
        if (keep <= 0) {
            return 1;
        }
        if (keep > MAX_KEEP_BACKUPS) {
            return MAX_KEEP_BACKUPS;
        }
        return keep;
    }

    /** 备份份目录名：yyyyMMdd-HHmmss。只有这种目录才允许被清理删掉。 */
    static boolean isBackupStampName(String name) {
        return name != null && name.matches("^[0-9]{8}-[0-9]{6}$");
    }

    /** 只有本插件写出的备份份才能进清理名单：日期目录名 + 标记文件。 */
    static boolean isOurBackupStamp(File dir) {
        if (dir == null || !dir.isDirectory() || !isBackupStampName(dir.getName())) {
            return false;
        }
        return new File(dir, BACKUP_MARKER).isFile() || new File(dir, ADDED_MARKER).isFile();
    }

    /**
     * 项目名或流水线名缺失时会落到 unknown-* 目录。多条线可能共用这一层，
     * 清理仍进行，但保留份数按 FALLBACK_MAX_KEEP 封顶，避免盘只涨不降。
     */
    static boolean isSharedFallbackFolder(File pipelineDir) {
        if (pipelineDir == null) {
            return true;
        }
        if ("unknown-pipeline".equals(pipelineDir.getName())) {
            return true;
        }
        File project = pipelineDir.getParentFile();
        return project != null && "unknown-project".equals(project.getName());
    }

    /**
     * 只删除「这一条流水线备份目录」里、本插件留下的旧份。
     *
     * 不确定就不删：父目录对不上、和站点重叠、没有标记文件、规范化后跑出本目录、
     * 以及刚写好的这一份，一律跳过。单次最多删 MAX_CLEANUP_PER_RUN 份。
     * 发布已经成功，清不掉只是占盘，删错才是生产事故。
     */
    static void cleanupOldBackups(File stampDir, File backupRoot, File site, int keep, LogSink sink) {
        if (stampDir == null || backupRoot == null || keep <= 0) {
            return;
        }
        File parent;
        File root;
        File stamp;
        try {
            stamp = stampDir.getCanonicalFile();
            parent = stamp.getParentFile();
            if (parent == null) {
                sink.log("  备份清理已跳过：备份目录没有父路径，拒绝删除");
                return;
            }
            parent = parent.getCanonicalFile();
            root = backupRoot.getCanonicalFile();
            if (!isBackupStampName(stamp.getName())) {
                sink.log("  备份清理已跳过：当前备份目录名不是日期时间格式 " + stamp.getName());
                return;
            }
            if (!parent.getPath().startsWith(root.getPath() + File.separator)) {
                sink.log("  备份清理已跳过：父目录不在备份根下 " + parent.getPath());
                return;
            }
            String rel = parent.getPath().substring(root.getPath().length() + 1);
            int seps = 0;
            for (int i = 0; i < rel.length(); i++) {
                char c = rel.charAt(i);
                if (c == File.separatorChar || c == '/') {
                    seps++;
                }
            }
            if (seps != 1) {
                sink.log("  备份清理已跳过：父目录不是「项目/流水线」两层 " + rel);
                return;
            }
            if (parent.getParentFile() == null || parent.getParentFile().getParentFile() == null) {
                sink.log("  备份清理已跳过：拒绝在盘符根或过浅的目录删除 " + parent.getPath());
                return;
            }
            if (isSharedFallbackFolder(parent)) {
                keep = Math.min(keep, FALLBACK_MAX_KEEP);
                sink.log("  备份落在 unnamed 目录，按更严的保留份数 " + keep
                        + " 清理，避免 unknown-project 把盘写满");
            }
            if (site != null) {
                File siteCanon = site.getCanonicalFile();
                String sitePath = siteCanon.getPath();
                String parentPath = parent.getPath();
                if (parentPath.equals(sitePath)
                        || parentPath.startsWith(sitePath + File.separator)
                        || sitePath.equals(parentPath)
                        || sitePath.startsWith(parentPath + File.separator)) {
                    sink.log("  备份清理已跳过：备份目录与站点目录重叠，拒绝删除");
                    return;
                }
            }
        } catch (Exception e) {
            sink.log("  备份清理已跳过：" + e.getMessage());
            return;
        }

        File[] dirs = parent.listFiles();
        if (dirs == null) {
            return;
        }
        List<File> list = new ArrayList<File>();
        for (File f : dirs) {
            if (isOurBackupStamp(f)) {
                list.add(f);
            }
        }
        if (list.size() <= keep) {
            return;
        }
        java.util.Collections.sort(list, new java.util.Comparator<File>() {
            public int compare(File a, File b) {
                return a.getName().compareTo(b.getName());
            }
        });
        int remove = Math.min(list.size() - keep, MAX_CLEANUP_PER_RUN);
        int deleted = 0;
        String parentPath = parent.getPath();
        for (int i = 0; i < list.size() && deleted < remove; i++) {
            File victim;
            try {
                victim = list.get(i).getCanonicalFile();
            } catch (Exception e) {
                sink.log("  跳过无法解析的备份目录：" + list.get(i).getPath());
                continue;
            }
            // 刚写好的这一份永远不能删：机器时钟回拨时它会排到最旧，按名字排会误伤本次回滚点。
            if (victim.equals(stamp)) {
                continue;
            }
            if (!isOurBackupStamp(victim)) {
                sink.log("  跳过无备份标记的目录，未删除：" + victim.getPath());
                continue;
            }
            File victimParent = victim.getParentFile();
            if (victimParent == null || !victimParent.equals(parent)) {
                sink.log("  跳过规范化后不在本流水线目录下的路径，未删除：" + victim.getPath());
                continue;
            }
            if (!victim.getPath().startsWith(parentPath + File.separator)) {
                sink.log("  跳过越界目录，未删除：" + victim.getPath());
                continue;
            }
            if (victim.equals(parent) || victim.equals(root) || (site != null && victim.equals(site))) {
                sink.log("  跳过危险目录，未删除：" + victim.getPath());
                continue;
            }
            sink.log("  准备删除过期备份 " + victim.getPath());
            deleteTree(victim);
            if (victim.exists()) {
                sink.log("  删除未完成，已保留：" + victim.getPath());
                continue;
            }
            deleted++;
        }
        if (deleted > 0) {
            sink.log("  已清理 " + deleted + " 份过期备份（保留最近 " + keep + " 份，单次最多 " + MAX_CLEANUP_PER_RUN + " 份）");
        }
    }

    /**
     * 列出包内文件，同时拦住解压炸弹：条目过多或声明体积过大直接拒绝。
     *
     * 声明大小可以造假，真正生效的是解压时边写边数；这里先挡一道，
     * 避免还没开始写就把目录枚举撑爆内存。
     */
    private static List<String> listEntries(ZipFile zf) {
        List<String> out = new ArrayList<String>();
        long declared = 0;
        Enumeration<? extends ZipEntry> en = zf.entries();
        while (en.hasMoreElements()) {
            ZipEntry e = en.nextElement();
            if (e.isDirectory()) {
                continue;
            }
            if (out.size() >= MAX_ENTRIES) {
                throw new IllegalStateException(
                        "增量包内文件超过 " + MAX_ENTRIES + " 个，拒绝解压。请缩小发布范围或拆包");
            }
            long size = e.getSize();
            if (size > MAX_ENTRY_BYTES) {
                throw new IllegalStateException(
                        "包内文件 " + e.getName() + " 声明大小超过单文件上限 "
                                + (MAX_ENTRY_BYTES / 1024 / 1024) + "MB，已中止");
            }
            if (size > 0) {
                declared += size;
                if (declared > MAX_TOTAL_UNPACKED) {
                    throw new IllegalStateException(
                            "增量包声明的解压体积超过 " + (MAX_TOTAL_UNPACKED / 1024 / 1024 / 1024)
                                    + "GB，拒绝解压");
                }
            }
            out.add(e.getName());
        }
        return out;
    }

    /**
     * 单个文件解压后的体积上限。
     *
     * 挡的是解压炸弹：几百 KB 的包能解出上百 GB，把生产机磁盘写满。盘一满，
     * 站点连日志都写不出来，比发布失败严重得多。正常发布包里最大的也就是几百 MB
     * 的媒体或安装包，2G 这条线不会误伤。
     */
    private static final long MAX_ENTRY_BYTES = 2L * 1024 * 1024 * 1024;

    /** 单个增量包最多包含的文件数，防止 zip 用海量空条目把内存打满。 */
    private static final int MAX_ENTRIES = 50000;

    /** 单个增量包解压后的累计体积上限。 */
    private static final long MAX_TOTAL_UNPACKED = 8L * 1024 * 1024 * 1024;

    /** 站点盘至少再留这么多余量，避免发布把盘写到 0 字节。 */
    private static final long DISK_HEADROOM_BYTES = 512L * 1024 * 1024;

    /**
     * 估计即将写入的解压体积，供落盘前预检磁盘。
     *
     * 条目声明大小缺失或为 0 时，按压缩包体积的 8 倍估，仍受总量上限约束。
     */
    private static long estimateUnpacked(ZipFile zf, List<String> entries, long packedBytes) {
        long declared = 0;
        boolean unknown = false;
        for (int i = 0; i < entries.size(); i++) {
            ZipEntry e = zf.getEntry(entries.get(i));
            long size = e == null ? -1 : e.getSize();
            if (size < 0) {
                unknown = true;
            } else {
                declared += size;
            }
        }
        if (unknown || declared <= 0) {
            declared = Math.max(declared, packedBytes * 8L);
        }
        if (declared > MAX_TOTAL_UNPACKED) {
            return MAX_TOTAL_UNPACKED;
        }
        return declared;
    }

    /**
     * 落盘前看剩余空间。读不到或为 0 视为危险，拒绝继续。
     *
     * 以前把 0 当成「未知、放行」，虚拟盘一旦真的报 0，发布会把盘写到死，
     * 站点连日志都写不出。宁可这次失败，也不能拿生产盘去赌。
     */
    private static boolean enoughSpace(File dir, long needBytes, String label, LogSink sink) {
        if (dir == null || needBytes <= 0) {
            return true;
        }
        long free = readUsableSpace(dir);
        long need = needBytes + DISK_HEADROOM_BYTES;
        if (!hasEnoughSpace(free, needBytes)) {
            if (free <= 0) {
                sink.log("  " + label + " 读不到剩余空间（" + free
                        + "），拒绝继续，以免把盘写满");
            } else {
                sink.log("  " + label + " 剩余 " + (free / 1024 / 1024) + "MB，本次大约需要 "
                        + (need / 1024 / 1024) + "MB（含预留），拒绝继续，以免把盘写满");
            }
            return false;
        }
        return true;
    }

    /**
     * 剩余空间是否够本次写入。
     *
     * @param freeBytes 盘上可用字节；≤0 表示读失败或盘已满，一律不够
     * @param needBytes 即将写入的字节（不含预留）
     */
    static boolean hasEnoughSpace(long freeBytes, long needBytes) {
        if (needBytes <= 0) {
            return true;
        }
        if (freeBytes <= 0) {
            return false;
        }
        return freeBytes >= needBytes + DISK_HEADROOM_BYTES;
    }

    /**
     * 读目录所在卷的可用空间。目录还不存在时沿父路径往上找。
     */
    static long readUsableSpace(File dir) {
        File probe = dir;
        while (probe != null && !probe.exists()) {
            probe = probe.getParentFile();
        }
        if (probe == null) {
            return -1;
        }
        try {
            long n = java.nio.file.Files.getFileStore(probe.toPath()).getUsableSpace();
            if (n > 0) {
                return n;
            }
        } catch (Exception ignored) {
        }
        try {
            long n = probe.getUsableSpace();
            return n > 0 ? n : -1;
        } catch (Exception e) {
            return -1;
        }
    }

    /**
     * 解压单个文件到生产目录：先写同目录下的临时文件，写完整了再替换过去。
     *
     * 不能直接对 dest 开流——那一下就把线上文件截成 0 字节，之后只要磁盘满、
     * 包读坏、进程被杀，站点里就躺着一个半截的 DLL 或空的 web.config。
     * 站点带着空 DLL 起来，现象是 500，比发布失败难查得多。
     * 写临时文件的话，任何一步失败原文件都还是完整的旧版本，站点继续能服务。
     */
    static long extract(ZipFile zf, String name, File dest) throws Exception {
        return extract(zf, name, dest, 0L);
    }

    /**
     * 解压单个文件。deadlineMs>0 时超时立刻停，线上原件还在。
     */
    static long extract(ZipFile zf, String name, File dest, long deadlineMs) throws Exception {
        ZipEntry entry = zf.getEntry(name);
        if (entry == null) {
            throw new IllegalStateException("包内条目丢失：" + name);
        }
        // 声明的大小先挡一道，省得白写一遍盘。但它是包里自己写的，可以造假，
        // 所以下面边写边数才是真正生效的那道
        long declared = entry.getSize();
        if (declared > MAX_ENTRY_BYTES) {
            throw new IllegalStateException(
                    "包内文件 " + name + " 声明大小 " + (declared / 1024 / 1024) + "MB，超过单文件上限"
                            + (MAX_ENTRY_BYTES / 1024 / 1024) + "MB，已中止");
        }
        File tmp = new File(dest.getParentFile(), dest.getName() + TMP_SUFFIX);
        boolean moved = false;
        long written = 0;
        try {
            InputStream in = null;
            FileOutputStream out = null;
            try {
                in = zf.getInputStream(entry);
                out = new FileOutputStream(tmp);
                byte[] buf = new byte[IO_BUFFER_BYTES];
                int n;
                while ((n = in.read(buf)) > 0) {
                    if (deadlineMs > 0 && System.currentTimeMillis() > deadlineMs) {
                        throw new IllegalStateException("发送文件超时，已中止（线上文件未被改动）");
                    }
                    written += n;
                    if (written > MAX_ENTRY_BYTES) {
                        throw new IllegalStateException(
                                "包内文件 " + name + " 解压后超过 " + (MAX_ENTRY_BYTES / 1024 / 1024)
                                        + "MB 仍未结束，疑似解压炸弹，已中止（线上文件未被改动）");
                    }
                    out.write(buf, 0, n);
                }
                // 落盘再替换：只 close 不 flush 到磁盘的话，机器这时断电会留下一个
                // 长度对但内容是空洞的文件，那种坏法比半截文件更难发现
                out.getFD().sync();
            } finally {
                NodeExecutor.closeQuietly(in);
                if (out != null) {
                    try {
                        out.close();
                    } catch (Exception ignored) {
                        // ignore
                    }
                }
            }
            replace(tmp, dest);
            moved = true;
        } finally {
            // 没替换成功就把临时文件收掉，别在站点目录里留垃圾：它不在
            // _added_files.txt 里，回滚也不会帮着删
            if (!moved) {
                tmp.delete();
            }
        }
        return written;
    }

    /** 临时文件后缀。带 release 前缀是为了在站点目录里一眼看出是谁留下的。 */
    private static final String TMP_SUFFIX = ".rp-tmp";

    /**
     * 用临时文件替换目标文件，尽量走原子替换。
     *
     * 原子替换失败（跨卷、文件系统不支持）才退回「删了再改名」。退回的那条路上有个
     * 极短的窗口目标文件不存在，但这一步只在原子替换不可用时才走，且比「边写边截断」
     * 的窗口小得多——后者是整个写入过程都处于坏状态。
     */
    private static void replace(File tmp, File dest) throws Exception {
        java.nio.file.Path src = tmp.toPath();
        java.nio.file.Path dst = dest.toPath();
        try {
            java.nio.file.Files.move(src, dst,
                    java.nio.file.StandardCopyOption.REPLACE_EXISTING,
                    java.nio.file.StandardCopyOption.ATOMIC_MOVE);
            return;
        } catch (Exception ignored) {
            // 换非原子的方式再试
        }
        java.nio.file.Files.move(src, dst, java.nio.file.StandardCopyOption.REPLACE_EXISTING);
    }

    /**
     * 把文件写进站点：先写临时文件再替换，失败时线上原件还在。
     *
     * 回滚也会覆盖正在服务的 DLL / web.config，不能直接对目标开流截成 0 字节。
     */
    static void copyReplace(File src, File dst) throws Exception {
        mkdirs(dst.getParentFile());
        File tmp = new File(dst.getParentFile(), dst.getName() + TMP_SUFFIX);
        boolean moved = false;
        try {
            copy(src, tmp, 0L);
            replace(tmp, dst);
            moved = true;
        } finally {
            if (!moved) {
                tmp.delete();
            }
        }
    }

    private static void copy(File src, File dst, long deadlineMs) throws Exception {
        java.io.FileInputStream in = null;
        FileOutputStream out = null;
        try {
            in = new java.io.FileInputStream(src);
            out = new FileOutputStream(dst);
            byte[] buf = new byte[IO_BUFFER_BYTES];
            int n;
            while ((n = in.read(buf)) > 0) {
                if (deadlineMs > 0 && System.currentTimeMillis() > deadlineMs) {
                    throw new IllegalStateException("备份拷贝超时，已中止");
                }
                out.write(buf, 0, n);
            }
        } finally {
            NodeExecutor.closeQuietly(in);
            if (out != null) {
                try {
                    out.close();
                } catch (Exception ignored) {
                    // ignore
                }
            }
        }
    }

    /**
     * 整步超时截止时间。步骤没填 timeoutSeconds 时用 1 小时；超过上限按上限。
     */
    static long resolveDeadline(Map<String, Object> with) {
        int sec = with == null ? 0 : Json.integer(with.get("timeoutSeconds"));
        long ms = sec <= 0 ? DEFAULT_STEP_TIMEOUT_MS : sec * 1000L;
        if (ms > MAX_STEP_TIMEOUT_MS) {
            ms = MAX_STEP_TIMEOUT_MS;
        }
        return System.currentTimeMillis() + ms;
    }

    /**
     * 取消或超时。超时和取消都要停，不能把节点线程占死。
     *
     * @return true 调用方应立刻失败返回
     */
    static boolean stopped(BooleanSupplier cancelled, long deadlineMs, LogSink sink, String phase) {
        if (cancelled != null && cancelled.getAsBoolean()) {
            sink.log("  已取消，停止" + phase);
            return true;
        }
        if (deadlineMs > 0 && System.currentTimeMillis() > deadlineMs) {
            sink.log("  发送文件超时，停止" + phase + "，避免占死节点");
            return true;
        }
        return false;
    }

    /**
     * 清掉上次崩溃留下的增量包。节点并发为 1，packages 里不该同时躺着好几份 zip。
     */
    static void purgeStalePackages(File work, File keep) {
        if (work == null) {
            return;
        }
        if (!work.isDirectory() && !work.mkdirs() && !work.isDirectory()) {
            return;
        }
        File[] files = work.listFiles();
        if (files == null) {
            return;
        }
        for (int i = 0; i < files.length; i++) {
            File f = files[i];
            if (f == null || !f.isFile()) {
                continue;
            }
            String name = f.getName();
            if (!name.startsWith("task-") || !name.endsWith(".zip")) {
                continue;
            }
            if (keep != null && f.getName().equals(keep.getName())) {
                continue;
            }
            f.delete();
        }
    }

    private static void writeLines(File file, List<String> lines) throws Exception {
        StringBuilder sb = new StringBuilder();
        for (String l : lines) {
            sb.append(l).append("\r\n");
        }
        FileOutputStream out = new FileOutputStream(file);
        try {
            out.write(sb.toString().getBytes("UTF-8"));
        } finally {
            out.close();
        }
    }

    private static void mkdirs(File dir) throws Exception {
        mkdirs(dir, "请确认运行账号对该路径有写权限");
    }

    private static void mkdirs(File dir, String hint) throws Exception {
        if (dir != null && !dir.isDirectory() && !dir.mkdirs() && !dir.isDirectory()) {
            throw new IllegalStateException("创建目录失败：" + dir.getPath() + "。" + hint);
        }
    }

    private static void deleteTree(File f) {
        if (f == null || !f.exists()) {
            return;
        }
        if (f.isDirectory()) {
            File[] children = f.listFiles();
            if (children != null) {
                for (File c : children) {
                    deleteTree(c);
                }
            }
        }
        f.delete();
    }
}
