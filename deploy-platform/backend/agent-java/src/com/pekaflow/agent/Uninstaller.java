package com.pekaflow.agent;

import java.io.File;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 按平台心跳里的 should_uninstall 卸掉本机 Agent。
 *
 * 顺序：写下卸载标记 → 向平台报卸完 → 删登记凭据 → 停计划任务 / systemd。
 * 凭据必须在停守护之前删掉：Linux 的 disable --now 会把当前进程杀掉，
 * 停完再删就来不及。站点目录、备份根、自定义工作空间不删。
 *
 * 守护若没停掉，进程挂起不再心跳，避免 systemd / 计划任务把卸到一半的
 * Agent 重新拉起来冲掉「已卸载」状态。
 */
public final class Uninstaller {

    private Uninstaller() {
    }

    /** 工作目录里的卸载标记。守护若在卸完前把进程拉起来，入口看到它就不再注册。 */
    static final String MARKER_NAME = "uninstall.requested";

    /**
     * 卸载一旦开始，心跳线程必须退出。
     *
     * 挂起的进程如果还按 10 秒一次上报，平台会认为机器仍在线，
     * 「确认已卸载」会被挡住，名单卡在卸载中。
     */
    private static volatile boolean stopHeartbeat;

    /** 心跳循环用来判断该不该退出。 */
    static boolean shouldStopHeartbeat() {
        return stopHeartbeat;
    }

    /** 标记文件路径：和 jar 放在同一目录。 */
    static File markerFile(Config config) {
        File jar = Config.selfJar();
        File dir = jar != null ? jar.getParentFile() : new File(".");
        return new File(dir, MARKER_NAME);
    }

    /** 是否已经收到过卸载指令。启动时看到就跳过注册，继续把本机卸干净。 */
    static boolean markerExists(Config config) {
        return markerFile(config).isFile();
    }

    /**
     * 写下卸载标记，内容是 agentId，重启后还知道该向哪台机器的登记报卸完。
     */
    static void writeMarker(Config config) {
        File f = markerFile(config);
        try {
            File dir = f.getParentFile();
            if (dir != null && !dir.exists()) {
                dir.mkdirs();
            }
            java.nio.file.Files.write(
                    f.toPath(),
                    String.valueOf(config.agentId).getBytes(java.nio.charset.StandardCharsets.UTF_8));
        } catch (Exception e) {
            System.err.println("[agent] 无法写入卸载标记: " + e.getMessage());
        }
    }

    /** 从标记里读出上次的 agentId；读不到返回 0。 */
    static int agentIdFromMarker(Config config) {
        File f = markerFile(config);
        if (!f.isFile()) {
            return 0;
        }
        try {
            String text = new String(
                    java.nio.file.Files.readAllBytes(f.toPath()),
                    java.nio.charset.StandardCharsets.UTF_8).trim();
            return Integer.parseInt(text);
        } catch (Exception ignored) {
            return 0;
        }
    }

    /**
     * 执行卸载。调用方应已排空任务。
     *
     * @param api 用来向平台报 uninstalled；token 缺失时只做本机清理
     */
    static void run(Config config, ApiClient api) {
        stopHeartbeat = true;
        System.out.println("[agent] 开始卸载本机 Agent");
        writeMarker(config);
        if (config.agentId <= 0) {
            config.agentId = agentIdFromMarker(config);
        }
        reportUninstalled(config, api);
        deleteCredentials(config);
        if (!stopWatchdog(config)) {
            System.err.println("[agent] 守护没停掉，进程挂起以免被重新拉起。请到机器上手动停服务。");
            parkForever();
        }
        System.out.println("[agent] 本机守护已停、登记凭据已删。站点目录和备份未动。");
    }

    /** 最后一次心跳带 uninstalled=true，名单变成「已卸载」，此时才允许页面删除。 */
    private static void reportUninstalled(Config config, ApiClient api) {
        if (api == null || config.agentId <= 0 || config.token == null || config.token.isEmpty()) {
            return;
        }
        try {
            Map<String, Object> hb = new HashMap<String, Object>();
            hb.put("uninstalled", Boolean.TRUE);
            hb.put("running_count", Integer.valueOf(0));
            hb.put("version", config.version);
            api.post("/api/v1/agents/" + config.agentId + "/heartbeat", hb);
            System.out.println("[agent] 已向平台报告卸载完成");
        } catch (Exception e) {
            System.err.println("[agent] 向平台报告卸载失败（本机仍会卸）: " + e.getMessage());
        }
    }

    /**
     * 停掉开机自启。
     *
     * @return 守护命令成功退出则为 true；失败时调用方应挂起进程，不能直接退出
     */
    private static boolean stopWatchdog(Config config) {
        boolean windows = "windows".equals(config.os);
        boolean node = "node".equals(config.role);
        if (windows) {
            String task = node ? "RELEASE-Node-Agent" : "RELEASE-Build-Agent";
            return runQuiet("schtasks.exe", "/Delete", "/TN", task, "/F");
        }
        if (node) {
            if (runQuiet("sudo", "-n", "systemctl", "disable", "--now", "rp-node")) {
                return true;
            }
            return runQuiet("systemctl", "disable", "--now", "rp-node");
        }
        return runQuiet("systemctl", "disable", "--now", "release-agent");
    }

    /** 删掉续期 token，避免卸完后被拉起来又自动登记回来。 */
    private static void deleteCredentials(Config config) {
        deleteQuietly(config.credentialFile());
        File legacy = new File(
                new File(System.getProperty("user.home", "."), ".release-agent"),
                "enrolled");
        File[] kids = legacy.isDirectory() ? legacy.listFiles() : null;
        if (kids != null) {
            for (int i = 0; i < kids.length; i++) {
                if (kids[i].isFile() && kids[i].getName().endsWith(".token")) {
                    deleteQuietly(kids[i]);
                }
            }
        }
    }

    /**
     * 守护没停掉时挂起：进程还在，systemd Restart=always / 计划任务就不会再拉一份。
     * 不再心跳，平台侧保持「已卸载」。
     */
    private static void parkForever() {
        while (true) {
            try {
                Thread.sleep(3600000L);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
    }

    /** 执行停守护命令。成功（退出码 0）返回 true。 */
    private static boolean runQuiet(String... cmd) {
        try {
            ProcessBuilder pb = new ProcessBuilder(cmd);
            pb.redirectErrorStream(true);
            Process p = pb.start();
            return p.waitFor() == 0;
        } catch (Exception e) {
            System.err.println("[agent] 停守护失败（" + join(cmd) + "）: " + e.getMessage());
            return false;
        }
    }

    private static String join(String[] cmd) {
        List<String> parts = new ArrayList<String>();
        for (int i = 0; i < cmd.length; i++) {
            parts.add(cmd[i]);
        }
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < parts.size(); i++) {
            if (i > 0) {
                sb.append(' ');
            }
            sb.append(parts.get(i));
        }
        return sb.toString();
    }

    private static void deleteQuietly(File f) {
        if (f == null || !f.exists()) {
            return;
        }
        try {
            if (!f.delete()) {
                System.err.println("[agent] 未能删除 " + f.getPath());
            }
        } catch (Exception ignored) {
            // 卸到这一步失败也不该把进程卡住
        }
    }
}
