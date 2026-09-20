package com.pekaflow.agent;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.text.SimpleDateFormat;
import java.util.Date;

/**
 * Agent 自升级：先把新 jar 下到旁边并校验指纹，再退出让守护进程拉起。
 *
 * Windows 上 JVM 锁住正在运行的 jar，进程内覆盖会失败，所以只准备
 * deploy-agent.jar.new，由 watchdog.ps1 在 JVM 起来之前完成替换。
 * Linux / macOS 上 jar 只是被 mmap，目录项可以立刻换成新文件，进程退出后
 * systemd 再拉起就是新版本。旧安装脚本若没有 ExecStartPre，Restart=always
 * 会用旧 jar 立刻起来，.new 永远换不上，所以 Unix 必须在进程内覆盖。
 *
 * 平台不会在有任务在跑的时候升级：升级信号只是让 Agent 停止领新任务，
 * 等手上的活干完（running 归零）才下载、退出。
 *
 * 退出前会留一张「换包回执」。Windows 那步替换不在本进程掌控内（脚本可能是旧版、
 * 可能被杀软拦下、可能没权限改文件），失败了本进程无从得知，重启后又会收到同样的
 * 升级指令，于是下载-退出-换包失败-旧版起来，无限循环。回执让重启后的进程能认出
 * 「上次换包没成功」，就此打住并把原因报给平台；管理员点「重试升级」会带
 * force_upgrade，越过熔断再下一遍。
 */
public final class SelfUpdater {

    private SelfUpdater() {
    }

    /** 暂存文件名，需与 watchdog.ps1 / systemd 里的替换逻辑保持一致。 */
    public static final String STAGED_NAME = "deploy-agent.jar.new";

    /** 换包回执：内容是「目标指纹 时间」，换包成功后由重启的进程自己清掉。 */
    private static final String ATTEMPT_NAME = "deploy-agent.upgrade-attempt";

    /**
     * 下载新 jar 并校验指纹。Unix 上随即覆盖当前 jar 的目录项；Windows 只留下 .new。
     *
     * @param expected 平台给出的目标指纹，用于确认下到的确实是那一版
     * @return null 表示已就绪（调用方应随即退出进程）；否则是失败原因
     */
    public static String stage(ApiClient api, String expected) {
        File current = Config.selfJar();
        if (current == null) {
            return "当前不是从 jar 启动（开发态），无法自升级";
        }
        File staged = new File(current.getParentFile(), STAGED_NAME);
        try {
            api.downloadToFile("/api/v1/agents/download", staged);
        } catch (Exception e) {
            deleteQuietly(staged);
            return "下载新 jar 失败：" + e.getMessage();
        }

        String got = Config.fingerprint(staged);
        // 指纹对不上说明下到的不是平台当前那一版（比如下载被截断、或平台正好在换包）。
        // 这时候换上去反而危险，不如原地不动，下个心跳再试
        if (expected != null && !expected.isEmpty() && !expected.equals(got)) {
            deleteQuietly(staged);
            return "新 jar 指纹不符（期望 " + expected + "，实际 " + got + "）";
        }
        if (got.equals(Config.fingerprintSelf())) {
            deleteQuietly(staged);
            return "下到的 jar 和当前版本一致，无需升级";
        }

        try {
            writeAttempt(current.getParentFile(), got);
        } catch (IOException e) {
            deleteQuietly(staged);
            return "无法写入换包回执（目录不可写？）：" + e.getMessage();
        }

        // Linux / macOS 上正在运行的 jar 只是被 mmap，目录项可以换成新文件，
        // 进程退出后 systemd/nohup 再拉起就是新版本。不必等 ExecStartPre。
        // 旧安装脚本没有 ExecStartPre 时，Restart=always 会立刻用旧 jar 起来，
        // 新包永远停在 .new 里，页面上就是「升级卡住了」。
        if (!isWindows()) {
            try {
                replaceCurrent(current, staged);
                System.out.println("[agent] 新版本 " + got + " 已覆盖当前 jar，退出后由守护进程拉起");
                return null;
            } catch (IOException e) {
                return "无法覆盖当前 jar：" + e.getMessage();
            }
        }

        System.out.println("[agent] 新版本 " + got + " 已就绪，退出等待守护进程完成替换");
        return null;
    }

    /**
     * 启动时如果旁边还留着上次没换上的 .new，Unix 上当场覆盖再退出。
     *
     * @return true 表示已经覆盖，调用方应立刻 System.exit，让守护进程拉起新文件
     */
    public static boolean takeoverStaged() {
        if (isWindows()) {
            return false;
        }
        File current = Config.selfJar();
        if (current == null) {
            return false;
        }
        File staged = new File(current.getParentFile(), STAGED_NAME);
        if (!staged.isFile()) {
            return false;
        }
        String got = Config.fingerprint(staged);
        if (got.equals(Config.fingerprintSelf()) || "unknown".equals(got)) {
            deleteQuietly(staged);
            return false;
        }
        try {
            replaceCurrent(current, staged);
            System.out.println("[agent] 发现未换上的 " + STAGED_NAME + "（" + got + "），已覆盖当前 jar，退出拉起");
            return true;
        } catch (IOException e) {
            System.err.println("[agent] 覆盖当前 jar 失败：" + e.getMessage());
            return false;
        }
    }

    /**
     * 启动时核对上一次换包的结果。
     *
     * @param selfVersion 本进程实际跑的 jar 指纹
     * @return null 表示一切正常；否则是上次换包失败的说明，应上报平台并停止自动重试
     */
    public static String consumeFailedAttempt(String selfVersion) {
        File jar = Config.selfJar();
        if (jar == null) {
            return null;
        }
        File dir = jar.getParentFile();
        File attempt = new File(dir, ATTEMPT_NAME);
        if (!attempt.isFile()) {
            return null;
        }
        String target = "";
        String when = "";
        try {
            String[] parts = new String(Files.readAllBytes(attempt.toPath()),
                    StandardCharsets.UTF_8).trim().split("\\s+", 2);
            target = parts.length > 0 ? parts[0] : "";
            when = parts.length > 1 ? parts[1] : "";
        } catch (IOException ignored) {
            // 读不出来就当作一次失败处理，反正回执存在本身就说明升级过
        }

        // 版本已经是目标版：换包成功了，回执使命结束
        if (!target.isEmpty() && target.equals(selfVersion)) {
            deleteQuietly(attempt);
            deleteQuietly(new File(dir, STAGED_NAME));
            return null;
        }

        deleteQuietly(attempt);
        boolean stagedLeft = new File(dir, STAGED_NAME).isFile();
        return "上次升级到 " + target + (when.isEmpty() ? "" : "（" + when + "）")
                + " 没有成功：进程重启后版本仍是 " + selfVersion
                + (stagedLeft ? "，新 jar 还留在 " + STAGED_NAME + " 未被换上" : "，新 jar 也不见了")
                + "。多半是守护进程（watchdog.ps1 / systemd）是旧版本或没权限替换文件，"
                + "请到这台机器上重跑安装脚本";
    }

    /**
     * 用暂存包覆盖当前 jar 的目录项。正在跑的进程仍握着旧 inode，退出后才真正切走。
     *
     * @param current 当前正在使用的 jar
     * @param staged  已校验过的 deploy-agent.jar.new
     */
    private static void replaceCurrent(File current, File staged) throws IOException {
        Files.move(staged.toPath(), current.toPath(),
                java.nio.file.StandardCopyOption.REPLACE_EXISTING);
    }

    /**
     * 是否跑在 Windows 上。Windows 锁 jar，只能走 .new + watchdog；Unix 可以覆盖目录项。
     */
    private static boolean isWindows() {
        return System.getProperty("os.name", "").toLowerCase().contains("win");
    }

    private static void writeAttempt(File dir, String target) throws IOException {
        String stamp = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss").format(new Date());
        Files.write(new File(dir, ATTEMPT_NAME).toPath(),
                (target + " " + stamp).getBytes(StandardCharsets.UTF_8));
    }

    private static void deleteQuietly(File f) {
        try {
            if (f.exists() && !f.delete()) {
                f.deleteOnExit();
            }
        } catch (Exception ignored) {
            // 删不掉也无所谓，下次升级会覆盖
        }
    }
}
