package com.pekaflow.agent;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * 任务执行器：执行 Job 的步骤。
 *
 * Agent 只负责起进程：
 *   - 已安装插件：下载 zip，执行 task.json 声明的入口命令
 *   - 内置命令：shell / bat / maven
 * 业务逻辑（如 git-checkout）在插件包内，不内嵌本类。
 *
 * Workspace：workspaceRoot/p-{workspace_uuid}/src/
 */
public class TaskExecutor implements StepRunner {

    /** 步骤边界标记：页面按它把 Job 日志切成每个步骤各自的段落，展示时会隐藏这两行 */
    public static final String STEP_BEGIN = "##[step]begin:";
    public static final String STEP_END = "##[step]end:";

    private final String workspaceRoot;
    private final ApiClient api;
    private final int agentId;
    private volatile Process currentProcess;
    private volatile boolean cancelled = false;
    private volatile String cancelledReason = "";
    private volatile String lastSourceRef = "";
    private int pipelineId;
    private int taskId;
    private int releaseId;
    private String jobName = "";
    /** 随任务下发的一次性凭证，只代表这个任务，注入给插件进程用 */
    private String taskToken = "";

    public TaskExecutor(String workspaceRoot, ApiClient api, int agentId) {
        this.workspaceRoot = workspaceRoot;
        this.api = api;
        this.agentId = agentId;
    }

    public String getLastSourceRef() {
        return lastSourceRef == null ? "" : lastSourceRef;
    }

    public boolean executeTask(Map<String, Object> task, LogSink sink) {
        cancelled = false;
        cancelledReason = "";
        currentProcess = null;
        lastSourceRef = "";

        this.pipelineId = Json.integer(task.get("pipeline_id"));
        this.taskId = Json.integer(task.get("task_id"));
        this.releaseId = Json.integer(task.get("release_id"));
        this.jobName = Json.str(task.get("job_name"));
        this.taskToken = Json.str(task.get("task_token"));
        String ws = Json.str(task.get("workspace"));
        if (ws == null || ws.trim().isEmpty()) {
            ws = "p-" + pipelineId;
        }
        File pipelineDir = new File(workspaceRoot, ws);
        File srcDir = new File(pipelineDir, "src");
        srcDir.mkdirs();

        sink.log("  Workspace: " + srcDir.getAbsolutePath());

        List<Object> steps = Json.arr(task.get("steps"));
        if (steps.isEmpty()) {
            sink.log("  (task has no steps)");
            return true;
        }

        boolean allOk = true;
        for (int i = 0; i < steps.size(); i++) {
            Map<String, Object> step = Json.obj(steps.get(i));
            String plugin = Json.str(step.get("plugin"));
            if (cancelled) {
                sink.log("  cancelled (" + cancelledReason + "), stop remaining steps");
                allOk = false;
                break;
            }
            Map<String, Object> with = Json.obj(step.get("with"));
            // 结构化边界：页面靠这对标记把整条 Job 日志切成每个步骤各自的段落
            sink.log(STEP_BEGIN + i + ":" + plugin);
            sink.log("  step: " + plugin);
            long startedAt = System.currentTimeMillis();
            reportStep(i, plugin, "running", startedAt, 0);

            boolean ok = executeStep(plugin, with, step, pipelineDir, srcDir, sink);
            long costMs = System.currentTimeMillis() - startedAt;
            String status = ok ? "success" : (cancelled ? "cancelled" : "failed");
            sink.log(STEP_END + i + ":" + status + ":" + costMs);
            reportStep(i, plugin, status, startedAt, costMs);

            if (!ok) {
                if (cancelled) {
                    sink.log("  step aborted by cancel (" + cancelledReason + ")");
                    allOk = false;
                    break;
                }
                sink.log("    step failed, job failed");
                allOk = false;
                break;
            }
        }
        currentProcess = null;
        return allOk;
    }

    /**
     * 把单个步骤的状态和耗时报给平台。
     *
     * 以前只有 Job 级起止时间，页面上每个步骤显示的都是整个 Job 的总耗时。
     * 上报失败不影响构建：日志里那对 ##[step] 标记仍能让页面还原出步骤边界。
     */
    private void reportStep(int index, String plugin, String status, long startedAtMs, long costMs) {
        if (taskId <= 0 || agentId <= 0) {
            return;
        }
        try {
            Map<String, Object> body = new java.util.HashMap<>();
            body.put("index", index);
            body.put("plugin", plugin == null ? "" : plugin);
            body.put("status", status);
            body.put("started_at_ms", startedAtMs);
            body.put("duration_ms", costMs);
            api.post("/api/v1/agents/" + agentId + "/tasks/" + taskId + "/step", body);
        } catch (Exception ignored) {
            // 步骤进度是锦上添花，网络抖动不该让构建跟着失败
        }
    }

    public void notifyCancelled(String reason) {
        cancelled = true;
        cancelledReason = reason == null ? "" : reason;
        Process p = currentProcess;
        if (p != null && p.isAlive()) {
            killProcessTree(p);
        }
    }

    private void killProcessTree(Process p) {
        try {
            p.destroyForcibly();
        } catch (Exception ignored) {
        }
        long pid = getProcessPid(p);
        if (pid <= 0) {
            return;
        }
        try {
            String os = System.getProperty("os.name", "").toLowerCase();
            ProcessBuilder pb;
            if (os.contains("win")) {
                pb = new ProcessBuilder("taskkill", "/F", "/T", "/PID", String.valueOf(pid));
            } else {
                pb = new ProcessBuilder(
                        "sh", "-c",
                        "kill -9 -" + pid + " 2>/dev/null; kill -9 " + pid
                                + " 2>/dev/null; pkill -P " + pid + " 2>/dev/null; true");
            }
            pb.inheritIO();
            Process helper = pb.start();
            helper.waitFor(5, java.util.concurrent.TimeUnit.SECONDS);
        } catch (Exception ignored) {
        }
    }

    /**
     * 取出子进程 PID，用来杀进程树。
     *
     * 取不到就返回 -1：调用方只会再走 destroyForcibly。
     * 绝不能回退到 RuntimeMXBean——那是 Agent 自己的 PID，Windows JDK8
     * 的 ProcessImpl 没有 pid 字段时一旦回退，超时会把构建机 Agent 打死。
     */
    static long getProcessPid(Process p) {
        if (p == null) {
            return -1;
        }
        try {
            java.lang.reflect.Method m = Process.class.getMethod("pid");
            Object v = m.invoke(p);
            if (v instanceof Number) {
                long pid = ((Number) v).longValue();
                if (pid > 0) {
                    return pid;
                }
            }
        } catch (Throwable ignored) {
        }
        try {
            java.lang.reflect.Field f = p.getClass().getDeclaredField("pid");
            f.setAccessible(true);
            Object v = f.get(p);
            if (v instanceof Number) {
                long pid = ((Number) v).longValue();
                if (pid > 0) {
                    return pid;
                }
            }
        } catch (Throwable ignored) {
        }
        return -1;
    }

    private boolean executeStep(String plugin, Map<String, Object> with, Map<String, Object> step,
                                File pipelineDir, File srcDir, LogSink sink) {
        Map<String, Object> pkg = Json.obj(step.get("package"));
        if (pkg != null && !pkg.isEmpty() && Json.str(pkg.get("name")).length() > 0) {
            return runAtomPlugin(pkg, with, pipelineDir, srcDir, sink);
        }
        switch (plugin) {
            case "shell-exec":
            case "bat-exec": {
                String content = firstNonEmpty(with, "content", "script", "");
                sink.log("    $ (cd " + srcDir.getName() + ") " + content);
                return runCommandIn(srcDir, content, sink);
            }
            case "maven-build": {
                // 有插件包时走上面的 zip；这里只给还没换包的旧任务兜底。
                // systemd 的 PATH 很窄，优先仓库 mvnw，再靠 enrichToolPath 去常见目录找 mvn。
                boolean win = System.getProperty("os.name", "").toLowerCase().contains("win");
                File wrapper = new File(srcDir, win ? "mvnw.cmd" : "mvnw");
                String mvn = wrapper.isFile() ? wrapper.getAbsolutePath() : "mvn";
                String goal = firstNonEmpty(with, "goal");
                if (goal.isEmpty()) {
                    goal = "package";
                }
                String cmd = mvn + " " + goal;
                if (Json.bool(with.get("skipTests"))) {
                    cmd += " -DskipTests";
                }
                sink.log("    $ " + cmd);
                return runCommandIn(srcDir, cmd, sink);
            }
            default:
                sink.log("    plugin " + plugin + " not installed (upload zip in Store and install)");
                return false;
        }
    }

    /** Download installed plugin zip and run entrypoint. Agent does not embed plugin logic. */
    private boolean runAtomPlugin(Map<String, Object> pkg, Map<String, Object> with,
                                  File pipelineDir, File srcDir, LogSink sink) {
        String name = Json.str(pkg.get("name"));
        String version = Json.str(pkg.get("version"));
        if (version == null || version.isEmpty()) {
            version = "1.0.0";
        }
        if (!isSafePluginIdentity(name) || !isSafePluginIdentity(version)) {
            sink.log("    插件标识或版本不合法，拒绝下载（不能含路径）");
            return false;
        }
        String entry = Json.str(pkg.get("entrypoint"));
        if (entry == null || entry.trim().isEmpty()) {
            entry = "python task.py";
        }
        String downloadPath = Json.str(pkg.get("download_path"));
        if (downloadPath == null || downloadPath.isEmpty()) {
            downloadPath = "/api/v1/store/plugins/" + name + "/package";
        }
        if (!isPluginDownloadPath(downloadPath)) {
            sink.log("    插件下载地址不是本平台商店路径，拒绝");
            return false;
        }
        String expectedSha = Json.str(pkg.get("sha256"));
        if (expectedSha == null || !expectedSha.trim().matches("(?i)[a-f0-9]{64}")) {
            sink.log("    插件包没有 sha256，拒绝运行");
            return false;
        }
        String home = System.getProperty("user.home", ".");
        File pluginDir = new File(new File(new File(home, ".release-agent/plugins"), name), version);
        File marker = new File(pluginDir, ".release_ready");
        File zip = new File(new File(home, ".release-agent/plugin-cache"), name + "-" + version + ".zip");
        boolean needFetch = !marker.exists();
        if (expectedSha != null && !expectedSha.trim().isEmpty()) {
            // 任务带了包哈希：缓存 zip 必须对上，不对就删掉重下，避免用被换过的旧包
            if (!zipMatches(zip, expectedSha)) {
                needFetch = true;
                deleteRecursively(pluginDir);
                if (zip.exists() && !zip.delete()) {
                    sink.log("    无法删除校验失败的插件缓存 " + zip.getAbsolutePath());
                    return false;
                }
            }
        }
        if (needFetch) {
            sink.log("    download plugin " + name + "@" + version);
            try {
                File parent = zip.getParentFile();
                if (parent != null) {
                    parent.mkdirs();
                }
                api.downloadToFile(downloadPath, zip);
                if (expectedSha != null && !expectedSha.trim().isEmpty() && !zipMatches(zip, expectedSha)) {
                    zip.delete();
                    sink.log("    plugin sha256 mismatch after download, refuse to run");
                    return false;
                }
                unzip(zip, pluginDir);
                marker.getParentFile().mkdirs();
                java.io.FileWriter w = new java.io.FileWriter(marker);
                w.write("ok");
                w.close();
            } catch (Exception e) {
                sink.log("    download/unzip failed: " + e.getMessage());
                return false;
            }
        } else {
            sink.log("    plugin cache " + pluginDir.getAbsolutePath());
        }
        sink.log("    $ " + entry);
        try {
            ProcessBuilder pb;
            String os = System.getProperty("os.name", "").toLowerCase();
            if (os.contains("win")) {
                if (entry.startsWith("python3 ")) {
                    entry = "python " + entry.substring("python3 ".length());
                }
                pb = new ProcessBuilder("cmd", "/C", entry);
            } else {
                pb = new ProcessBuilder("bash", "-c", entry);
            }
            pb.directory(pluginDir);
            pb.redirectErrorStream(true);
            Map<String, String> env = pb.environment();
            enrichToolPath(env);
            env.put("GIT_TERMINAL_PROMPT", "0");
            env.put("GIT_ASKPASS", "echo");
            // Windows Git Credential Manager 会弹 GUI，GIT_TERMINAL_PROMPT=0 挡不住
            env.put("GCM_INTERACTIVE", "never");
            env.put("GCM_MODAL_PROMPT", "false");
            env.put("GCM_GUI_PROMPT", "false");
            env.put("RELEASE_WORKSPACE", pipelineDir.getAbsolutePath());
            env.put("RELEASE_SRC", srcDir.getAbsolutePath());
            env.put("BK_CI_WORKSPACE", pipelineDir.getAbsolutePath());
            env.put("RELEASE_ATOM_INPUT_JSON", Json.stringify(with));
            env.put("RELEASE_PIPELINE_ID", String.valueOf(pipelineId));
            env.put("RELEASE_BUILD_ID", String.valueOf(taskId));
            env.put("RELEASE_RELEASE_ID", String.valueOf(releaseId));
            env.put("RELEASE_JOB_NAME", jobName == null ? "" : jobName);
            env.put("RELEASE_SERVER_URL", api.serverUrl());
            // 只给插件本次任务的凭证。Agent 自己的长期 token 一旦泄进插件进程，
            // 插件就能领走别人的任务、伪造任务状态，所以这里不注入。
            env.put("RELEASE_TASK_TOKEN", taskToken == null ? "" : taskToken);
            env.put("RELEASE_AGENT_ID", String.valueOf(api.agentId()));
            env.put("PYTHONUNBUFFERED", "1");
            // 下面按 UTF-8 读插件输出，而 Windows 上 Python 默认按 ANSI 代码页写，
            // 不显式对齐的话中文日志会是乱码
            env.put("PYTHONIOENCODING", "utf-8");
            final Process process = pb.start();
            currentProcess = process;
            final Thread reader = new Thread(() -> {
                try (BufferedReader br = new BufferedReader(
                        new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8))) {
                    String line;
                    while ((line = br.readLine()) != null) {
                        sink.log("      " + line);
                        if (line.startsWith("##[set-output]source_ref=")) {
                            lastSourceRef = line.substring("##[set-output]source_ref=".length()).trim();
                        }
                    }
                } catch (Exception ignored) {
                }
            });
            reader.setDaemon(true);
            reader.start();
            boolean done = process.waitFor(600, java.util.concurrent.TimeUnit.SECONDS);
            if (!done) {
                killProcessTree(process);
                sink.log("    plugin timeout");
                return false;
            }
            if (cancelled) {
                sink.log("    plugin cancelled (" + cancelledReason + ")");
                return false;
            }
            reader.join(15000);
            int code = process.exitValue();
            sink.log("    (exit " + code + ")");
            pickSourceRefFromOutput(pipelineDir, pluginDir, sink);
            return code == 0;
        } catch (Exception e) {
            sink.log("    plugin error: " + e.getMessage());
            return false;
        } finally {
            currentProcess = null;
        }
    }

    private void pickSourceRefFromOutput(File pipelineDir, File pluginDir, LogSink sink) {
        File[] files = new File[] {
                new File(pipelineDir, ".release_atom_output.json"),
                new File(pluginDir, ".release_atom_output.json"),
        };
        for (File f : files) {
            if (!f.isFile()) {
                continue;
            }
            try {
                String text = new String(java.nio.file.Files.readAllBytes(f.toPath()), StandardCharsets.UTF_8);
                Object root = Json.parse(text);
                Map<String, Object> data = Json.obj(Json.obj(root).get("data"));
                if (data == null) {
                    continue;
                }
                Object sr = data.get("source_ref");
                if (sr instanceof Map) {
                    @SuppressWarnings("unchecked")
                    String v = Json.str(((Map<String, Object>) sr).get("value"));
                    if (v != null && !v.isEmpty()) {
                        lastSourceRef = v;
                        sink.log("    source_ref=" + lastSourceRef);
                        return;
                    }
                }
            } catch (Exception ignored) {
            }
        }
    }

    /**
     * 插件标识会拼进缓存目录。只允许字母数字和 ._- ，避免 name=../../.ssh 把 zip 解到别处。
     */
    static boolean isSafePluginIdentity(String s) {
        return s != null && s.matches("^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$");
    }

    /**
     * 构建机只从本平台商店拉插件包。其它路径一律拒绝，包括 @host 这种会把 URL host 拐走的写法。
     */
    static boolean isPluginDownloadPath(String path) {
        if (path == null) {
            return false;
        }
        return path.matches("^/api/v1/store/plugins/[A-Za-z0-9][A-Za-z0-9._-]{0,62}/package$")
                || path.matches("^/api/v1/store/plugin-drafts/[1-9][0-9]{0,11}/package$");
    }

    /**
     * 缓存 zip 的 sha256 是否等于任务里带的 package.sha256。
     *
     * 文件不在或算不出哈希都视为不对，调用方删缓存重下。
     */
    static boolean zipMatches(File zip, String expectedSha) {
        if (zip == null || !zip.isFile() || expectedSha == null || expectedSha.trim().isEmpty()) {
            return false;
        }
        try {
            return sha256Hex(zip).equalsIgnoreCase(expectedSha.trim());
        } catch (Exception e) {
            return false;
        }
    }

    /** 文件内容的 sha256 hex。大 zip 分块读，避免一次性进内存。 */
    static String sha256Hex(File file) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        try (FileInputStream in = new FileInputStream(file)) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) {
                md.update(buf, 0, n);
            }
        }
        byte[] digest = md.digest();
        StringBuilder sb = new StringBuilder(digest.length * 2);
        for (byte b : digest) {
            sb.append(String.format("%02x", b));
        }
        return sb.toString();
    }

    private static void unzip(File zip, File dest) throws Exception {
        if (dest.exists()) {
            deleteRecursively(dest);
        }
        dest.mkdirs();
        try (java.util.zip.ZipInputStream zis = new java.util.zip.ZipInputStream(
                new java.io.FileInputStream(zip))) {
            java.util.zip.ZipEntry e;
            byte[] buf = new byte[8192];
            while ((e = zis.getNextEntry()) != null) {
                File out = NodeExecutor.safeChild(dest, e.getName());
                if (e.isDirectory()) {
                    out.mkdirs();
                } else {
                    File parent = out.getParentFile();
                    if (parent != null) {
                        parent.mkdirs();
                    }
                    try (java.io.FileOutputStream fos = new java.io.FileOutputStream(out)) {
                        int n;
                        while ((n = zis.read(buf)) > 0) {
                            fos.write(buf, 0, n);
                        }
                    }
                }
                zis.closeEntry();
            }
        }
    }

    private static void deleteRecursively(File f) {
        if (f.isDirectory()) {
            File[] kids = f.listFiles();
            if (kids != null) {
                for (File k : kids) {
                    deleteRecursively(k);
                }
            }
        }
        f.delete();
    }

    private boolean runCommandIn(File dir, String command, LogSink sink) {
        if (command.trim().isEmpty()) {
            sink.log("    (empty command)");
            return true;
        }
        try {
            ProcessBuilder pb;
            String os = System.getProperty("os.name", "").toLowerCase();
            if (os.contains("win")) {
                pb = new ProcessBuilder("cmd", "/C", command);
            } else {
                pb = new ProcessBuilder("bash", "-c", command);
            }
            pb.directory(dir);
            pb.redirectErrorStream(true);
            enrichToolPath(pb.environment());
            pb.environment().put("GIT_TERMINAL_PROMPT", "0");
            pb.environment().put("GIT_ASKPASS", "echo");
            // Windows Git Credential Manager 会弹 GUI，GIT_TERMINAL_PROMPT=0 挡不住
            pb.environment().put("GCM_INTERACTIVE", "never");
            pb.environment().put("GCM_MODAL_PROMPT", "false");
            pb.environment().put("GCM_GUI_PROMPT", "false");

            final Process process = pb.start();
            currentProcess = process;

            final Thread reader = new Thread(() -> {
                try (BufferedReader br = new BufferedReader(
                        new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8))) {
                    String line;
                    while ((line = br.readLine()) != null) {
                        sink.log("      " + line);
                    }
                } catch (Exception ignored) {
                }
            });
            reader.setDaemon(true);
            reader.start();

            long timeoutSeconds = 600;
            boolean done = process.waitFor(timeoutSeconds, java.util.concurrent.TimeUnit.SECONDS);
            if (!done) {
                killProcessTree(process);
                sink.log("    command timeout (>" + timeoutSeconds + "s)");
                return false;
            }
            if (cancelled) {
                sink.log("    command cancelled (" + cancelledReason + ")");
                return false;
            }
            reader.join(15000);
            int code = process.exitValue();
            sink.log("    (exit " + code + ")");
            return code == 0;
        } catch (Exception e) {
            sink.log("    command error: " + e.getMessage());
            return false;
        } finally {
            currentProcess = null;
        }
    }

    private static String firstNonEmpty(Map<String, Object> map, String... keys) {
        for (String k : keys) {
            Object v = map.get(k);
            if (v != null && !String.valueOf(v).trim().isEmpty()) {
                return String.valueOf(v);
            }
        }
        return "";
    }

    /**
     * 把本机常见的构建工具目录补进子进程 PATH。
     *
     * Agent 作为 systemd 服务启动时 PATH 只有 /usr/bin，交互登录能用的 mvn / java
     * 服务里找不到。除固定目录外，还会扫 /data/soft、/opt 下一层带 maven/jdk 的 bin。
     * 目录不存在的跳过，不覆盖调用方已经设好的 PATH。
     */
    static void enrichToolPath(Map<String, String> env) {
        if (env == null) {
            return;
        }
        String key = "PATH";
        String current = env.get(key);
        if (current == null) {
            for (String existing : env.keySet()) {
                if ("PATH".equalsIgnoreCase(existing)) {
                    key = existing;
                    current = env.get(existing);
                    break;
                }
            }
        }
        if (current == null) {
            current = "";
        }
        String home = System.getProperty("user.home", "");
        List<String> extras = new ArrayList<String>();
        extras.add("/usr/local/bin");
        extras.add("/opt/maven/bin");
        extras.add("/usr/share/maven/bin");
        extras.add("/usr/local/maven/bin");
        extras.add("/opt/gradle/bin");
        extras.add("/snap/bin");
        extras.add("/usr/share/gradle/bin");
        extras.add(home + "/.sdkman/candidates/maven/current/bin");
        extras.add(home + "/.sdkman/candidates/gradle/current/bin");
        extras.add(home + "/.nvm/current/bin");
        extras.add(home + "/.local/bin");
        extras.add(home + "/.dotnet");
        extras.add("C:\\Program Files\\nodejs");
        extras.add("C:\\Program Files\\Apache\\maven\\bin");
        extras.add("C:\\Program Files\\Maven\\bin");
        extras.add("C:\\Program Files\\Gradle\\bin");
        extras.add("C:\\Program Files\\Docker\\Docker\\resources\\bin");
        extras.add("C:\\Program Files\\Git\\cmd");
        extras.add("C:\\Program Files\\Git\\bin");
        extras.add("C:\\Program Files (x86)\\Git\\cmd");
        extras.add("C:\\Program Files\\dotnet");
        extras.add("C:\\Program Files\\Kubernetes\\Client\\bin");
        appendSoftInstallBins(extras, new File("/data/soft"));
        appendSoftInstallBins(extras, new File("/opt"));
        appendSoftInstallBins(extras, new File("/usr/local"));
        StringBuilder prepend = new StringBuilder();
        String sep = File.pathSeparator;
        String lower = current.toLowerCase();
        for (String dir : extras) {
            if (dir == null || dir.isEmpty()) {
                continue;
            }
            File folder = new File(dir);
            if (!folder.isDirectory()) {
                continue;
            }
            String abs = folder.getAbsolutePath();
            if (lower.contains(abs.toLowerCase())) {
                continue;
            }
            if (prepend.length() > 0) {
                prepend.append(sep);
            }
            prepend.append(abs);
        }
        String mavenHome = env.get("MAVEN_HOME");
        if (mavenHome == null) {
            mavenHome = env.get("M2_HOME");
        }
        if (mavenHome != null && !mavenHome.trim().isEmpty()) {
            File bin = new File(mavenHome.trim(), "bin");
            if (bin.isDirectory() && !lower.contains(bin.getAbsolutePath().toLowerCase())) {
                if (prepend.length() > 0) {
                    prepend.append(sep);
                }
                prepend.append(bin.getAbsolutePath());
            }
        }
        if (prepend.length() == 0) {
            return;
        }
        env.put(key, current.isEmpty() ? prepend.toString() : prepend + sep + current);
    }

    /**
     * 把 prefix 下一层目录里带 mvn/java/gradle 的 bin 补进列表。
     * 例如 /data/soft/maven/bin、/data/soft/jdk8/bin。只看一层，避免扫整盘。
     *
     * @param out    待补进 PATH 的目录列表
     * @param prefix 安装根，如 /data/soft
     */
    private static void appendSoftInstallBins(List<String> out, File prefix) {
        if (out == null || prefix == null || !prefix.isDirectory()) {
            return;
        }
        File[] kids = prefix.listFiles();
        if (kids == null) {
            return;
        }
        for (int i = 0; i < kids.length; i++) {
            File kid = kids[i];
            if (!kid.isDirectory()) {
                continue;
            }
            File bin = new File(kid, "bin");
            if (!bin.isDirectory()) {
                continue;
            }
            if (new File(bin, "mvn").isFile() || new File(bin, "mvn.cmd").isFile()
                    || new File(bin, "java").isFile() || new File(bin, "java.exe").isFile()
                    || new File(bin, "gradle").isFile() || new File(bin, "gradle.bat").isFile()
                    || new File(bin, "npm").isFile() || new File(bin, "npm.cmd").isFile()) {
                out.add(bin.getAbsolutePath());
            }
        }
    }
}
