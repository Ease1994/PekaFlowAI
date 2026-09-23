package com.pekaflow.agent;

import java.io.File;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 按平台心跳里的 should_uninstall 卸掉本机 Agent。
 *
 * 顺序：先留下卸载标记（守护拉起时还能接着卸）→ 向平台报卸完 → 停计划任务 /
 * systemd → 删登记凭据 → 退出。站点目录、备份根、自定义工作空间不删。
 */
public final class Uninstaller {

    private Uninstaller() {
    }

    /** 工作目录里的卸载标记。守护若在卸完前把进程拉起来，入口看到它就不再注册。 */
    static final String MARKER_NAME = "uninstall.requested";

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
        System.out.println("[agent] 开始卸载本机 Agent");
        writeMarker(config);
        if (config.agentId <= 0) {
            config.agentId = agentIdFromMarker(config);
        }
        reportUninstalled(config, api);
        stopWatchdog(config);
        deleteCredentials(config);
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

    /** 停掉开机自启。必须在删凭据之前，否则守护会带着旧凭据重新注册。 */
    private static void stopWatchdog(Config config) {
        boolean windows = "windows".equals(config.os);
        boolean node = "node".equals(config.role);
        if (windows) {
            String task = node ? "RELEASE-Node-Agent" : "RELEASE-Build-Agent";
            runQuiet("schtasks.exe", "/Delete", "/TN", task, "/F");
            return;
        }
        if (node) {
            runQuiet("sudo", "-n", "systemctl", "disable", "--now", "rp-node");
            runQuiet("systemctl", "disable", "--now", "rp-node");
            return;
        }
        runQuiet("systemctl", "disable", "--now", "release-agent");
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

    private static void runQuiet(String... cmd) {
        try {
            ProcessBuilder pb = new ProcessBuilder(cmd);
            pb.redirectErrorStream(true);
            Process p = pb.start();
            p.waitFor();
        } catch (Exception e) {
            System.err.println("[agent] 停守护失败（" + join(cmd) + "）: " + e.getMessage());
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
