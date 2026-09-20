package com.pekaflow.agent;

import java.io.File;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 部署节点执行器：跑在生产服务器上，只做内置的发布动作。
 *
 * 和构建机的 TaskExecutor 是两条路：这里不下载插件、不执行任意脚本、不碰代码仓库，
 * 只认 file-transfer、rollback-files 和服务启停（Windows 用 iis-control，
 * Linux 用 service-control）这几个动作，其余一律拒绝。所有落盘操作都必须在启动时
 * 用 --allow-paths 声明的目录内，这条线画在节点本地，平台下发什么都越不过去。
 */
public class NodeExecutor implements StepRunner {

    static final String STEP_BEGIN = "##[step]begin:";
    static final String STEP_END = "##[step]end:";

    /** 服务启停类步骤：发布前后停/起服务，回滚时要原样再来一遍。 */
    private static final String[] SERVICE_PLUGINS = {"iis-control", "service-control"};

    private final ApiClient api;
    private final int agentId;
    private final List<String> allowPaths;
    private final List<String> allowServices;
    private final List<String> allowIis;
    private final File agentHome;
    /** 安装时定下的备份根；流水线 backupDir 留空时用它。 */
    private final String backupRoot;

    private volatile boolean cancelled = false;
    private volatile String cancelledReason = "";

    public NodeExecutor(ApiClient api, int agentId, List<String> allowPaths,
                        List<String> allowServices, List<String> allowIis, File agentHome) {
        this(api, agentId, allowPaths, allowServices, allowIis, agentHome, "");
    }

    public NodeExecutor(ApiClient api, int agentId, List<String> allowPaths,
                        List<String> allowServices, List<String> allowIis, File agentHome,
                        String backupRoot) {
        this.api = api;
        this.agentId = agentId;
        this.allowPaths = allowPaths;
        this.allowServices = allowServices == null ? new ArrayList<String>() : allowServices;
        this.allowIis = allowIis == null ? new ArrayList<String>() : allowIis;
        this.agentHome = agentHome;
        this.backupRoot = backupRoot == null ? "" : backupRoot.trim();
    }

    public void notifyCancelled(String reason) {
        this.cancelled = true;
        this.cancelledReason = reason == null ? "" : reason;
    }

    /** 当前任务是否已被平台取消。启停等待循环用它中断，补偿启动故意不看这个标志。 */
    public boolean isCancelled() {
        return cancelled;
    }

    /** 节点不产出代码版本，保留接口是为了和 AgentMain 的上报流程一致。 */
    public String getLastSourceRef() {
        return null;
    }

    public boolean executeTask(Map<String, Object> task, LogSink sink) {
        int taskId = Json.integer(task.get("task_id"));
        List<Object> steps = Json.arr(task.get("steps"));
        if (steps.isEmpty()) {
            sink.log("任务里没有步骤");
            return false;
        }

        sink.log("节点：" + nodeLabel() + "，允许操作的目录：" + allowPaths);

        // 本任务里真正停下、还没对称启动的服务。失败或取消时按这份名单补 start，
        // 避免「停站点 → 传文件失败」把生产停在半路。只记实际发出的 stop，
        // 本来就是停止态的空操作不记，免得把运维主动停着的站点拉起来。
        List<PendingStart> pendingStarts = new ArrayList<PendingStart>();
        boolean ok = true;
        try {
            for (int i = 0; i < steps.size(); i++) {
                if (cancelled) {
                    sink.log("  已取消（" + cancelledReason + "），停止后续步骤");
                    ok = false;
                    break;
                }
                Map<String, Object> step = Json.obj(steps.get(i));
                String plugin = Json.str(step.get("plugin"));
                Map<String, Object> with = Json.obj(step.get("with"));

                sink.log(STEP_BEGIN + i + ":" + plugin);
                long startedAt = System.currentTimeMillis();
                reportStep(taskId, i, plugin, "running", startedAt, 0);

                StepOutcome outcome;
                try {
                    outcome = runStep(plugin, with, varMap(task.get("variables")), taskId, sink);
                } catch (Exception e) {
                    sink.log("  步骤异常：" + e.getMessage());
                    outcome = StepOutcome.fail();
                }
                if (outcome.issuedStop) {
                    pendingStarts.add(new PendingStart(plugin, with));
                }
                if (outcome.issuedStart) {
                    removePending(pendingStarts, plugin, with);
                }
                ok = outcome.ok;
                // 只要备份已经落地就上报，成功失败都报。失败恰恰是最需要回滚的时候：
                // 解压到一半断了，站点是新旧混合的坏状态，这时候平台上要是没有回滚记录，
                // 就只能让人登上生产机翻备份目录手工拷回去
                if ("file-transfer".equals(plugin)) {
                    reportDeployment(taskId, i, steps, with, sink);
                    if (!ok && lastBackupDir != null && !lastBackupDir.isEmpty()) {
                        restoreFailedTransfer(taskId, with, sink);
                    }
                }

                long cost = System.currentTimeMillis() - startedAt;
                String status = ok ? "success" : (cancelled ? "cancelled" : "failed");
                sink.log(STEP_END + i + ":" + status + ":" + cost);
                reportStep(taskId, i, plugin, status, startedAt, cost);

                if (!ok) {
                    sink.log("    步骤失败，终止本次发布");
                    break;
                }
            }
            return ok;
        } finally {
            if (!ok) {
                compensateStarts(pendingStarts, sink);
            }
        }
    }

    /**
     * 取本次构建的变量表（名字 -> 值）。
     *
     * 平台现在下发的就是这张表，但升级不是原子的：Agent 换了版本、平台还没换，或者队列里
     * 压着换版本前建好的任务，那时候拿到的是 [{name, value}, ...] 这种变量定义列表。
     * 直接按 Map 解析会当场 ClassCastException，把一次好好的发布判成失败，
     * 所以两种形状都认。
     */
    private static Map<String, Object> varMap(Object raw) {
        Map<String, Object> out = new HashMap<String, Object>();
        if (raw instanceof Map) {
            for (Map.Entry<?, ?> e : ((Map<?, ?>) raw).entrySet()) {
                out.put(String.valueOf(e.getKey()), e.getValue());
            }
            return out;
        }
        if (raw instanceof List) {
            for (Object item : (List<?>) raw) {
                if (!(item instanceof Map)) {
                    continue;
                }
                Map<String, Object> v = Json.obj(item);
                String name = Json.str(v.get("name"));
                if (name == null || name.isEmpty()) {
                    continue;
                }
                Object val = v.get("value");
                out.put(name, val != null ? val : v.get("default_value"));
            }
        }
        return out;
    }

    /**
     * 单步结果：是否成功，以及这次是否真正停下/拉起了服务。
     *
     * 成功与否不够用来做补偿：空操作的 stop 也返回成功，但不能据此去 start。
     */
    private static final class StepOutcome {
        final boolean ok;
        final boolean issuedStop;
        final boolean issuedStart;

        StepOutcome(boolean ok, boolean issuedStop, boolean issuedStart) {
            this.ok = ok;
            this.issuedStop = issuedStop;
            this.issuedStart = issuedStart;
        }

        static StepOutcome fail() {
            return new StepOutcome(false, false, false);
        }

        static StepOutcome of(boolean ok) {
            return new StepOutcome(ok, false, false);
        }
    }

    /** 失败时待补偿启动的一条服务。with 原样保留，只把 action 改成 start。 */
    private static final class PendingStart {
        final String plugin;
        final Map<String, Object> with;

        PendingStart(String plugin, Map<String, Object> with) {
            this.plugin = plugin;
            this.with = with;
        }
    }

    /**
     * 执行一个内置步骤。
     *
     * 启停类步骤把「是否真的发出了 stop/start」带回给 executeTask，
     * 用来维护待补偿名单；传文件步骤会检查取消标志，避免解压到一半还继续写盘。
     */
    private StepOutcome runStep(String plugin, Map<String, Object> with, Map<String, Object> vars,
                                int taskId, LogSink sink) throws Exception {
        if ("file-transfer".equals(plugin)) {
            FileTransfer ft = new FileTransfer(api, agentId, allowPaths, agentHome, backupRoot);
            // 上一步可能也是 file-transfer，不清掉会把它的备份目录当成这一步的记上去，
            // 回滚时还原到错误的目录
            lastBackupDir = "";
            lastBackupSummary = "";
            try {
                return StepOutcome.of(ft.run(with, vars, taskId, sink, this::isCancelled));
            } finally {
                // 必须放 finally：run() 抛异常时站点可能已经被改了一半，
                // 那份备份是唯一的退路，不能因为异常就把它的位置丢掉
                lastBackupDir = ft.getBackupDir();
                lastBackupSummary = ft.getSummary();
            }
        }
        if ("rollback-files".equals(plugin)) {
            return StepOutcome.of(new RollbackFiles(api, agentId, allowPaths, backupRoot).run(with, taskId, sink));
        }
        if ("iis-control".equals(plugin)) {
            IisControl ctl = new IisControl(allowPaths, allowIis);
            boolean ok = ctl.run(with, sink, this::isCancelled);
            return new StepOutcome(ok, ctl.issuedStop(), ctl.issuedStart());
        }
        if ("service-control".equals(plugin)) {
            ServiceControl ctl = new ServiceControl(allowServices);
            boolean ok = ctl.run(with, sink, this::isCancelled);
            return new StepOutcome(ok, ctl.issuedStop(), ctl.issuedStart());
        }
        // 白名单之外的一律拒绝：生产节点不是通用执行器
        sink.log("  节点不支持步骤「" + plugin + "」。部署节点只执行「发送文件到节点」「还原备份」"
                + "和服务启停（Windows 用 IIS 控制、Linux 用服务控制），"
                + "编译类步骤请放到构建机上的 Job 里");
        return StepOutcome.fail();
    }

    /**
     * 传文件失败时站点已经是新旧混合。补偿启服会把服务拉起来对外服务，
     * 所以先按本次备份还原；还原失败只打日志，启服仍然进行，避免站点一直停着。
     */
    private void restoreFailedTransfer(int taskId, Map<String, Object> with, LogSink sink) {
        sink.log("  传文件没有完成，先按本次备份还原，再补偿启服");
        try {
            Map<String, Object> rb = new HashMap<String, Object>();
            rb.put("targetDir", Json.str(with.get("targetDir")));
            rb.put("backupDir", lastBackupDir);
            boolean restored = new RollbackFiles(api, agentId, allowPaths, backupRoot)
                    .run(rb, taskId, sink);
            if (!restored) {
                sink.log("  自动还原没有成功，补偿启服后站点可能仍是混合状态，请到平台执行回滚");
            }
        } catch (Exception e) {
            sink.log("  自动还原失败：" + e.getMessage() + "，请到平台执行回滚");
        }
    }

    /**
     * 任务失败或取消后，把本任务已经停下、还没对称启动的服务拉起来。
     *
     * 典型编排是 stop → 传文件 → start。取消和失败都发生在步骤间隙或步骤内部，
     * 如果不补这一下，站点会一直停着直到有人登生产机。补偿失败只打日志，
     * 不再把任务结果改来改去——原失败原因更重要，启动失败要人上机器看。
     * 这里故意不看 cancelled：取消的正是后续步骤，恢复服务不能被它拦住。
     */
    private void compensateStarts(List<PendingStart> pending, LogSink sink) {
        if (pending == null || pending.isEmpty()) {
            return;
        }
        sink.log("  发布未完成，正在把本任务已停止的服务拉起来，避免站点停在半路");
        for (PendingStart item : pending) {
            Map<String, Object> startWith = new HashMap<String, Object>(item.with);
            startWith.put("action", "start");
            String name = Json.str(startWith.get("name"));
            try {
                boolean started;
                if ("iis-control".equals(item.plugin)) {
                    started = new IisControl(allowPaths, allowIis).run(startWith, sink, null);
                } else if ("service-control".equals(item.plugin)) {
                    started = new ServiceControl(allowServices).run(startWith, sink, null);
                } else {
                    continue;
                }
                if (!started) {
                    sink.log("  补偿启动「" + name + "」没有成功，请到服务器上手动确认服务状态");
                }
            } catch (Exception e) {
                sink.log("  补偿启动「" + name + "」失败：" + e.getMessage() + "，请到服务器上手动确认");
            }
        }
        pending.clear();
    }

    /**
     * 对称的 start 已经成功发出后，从待补偿名单里拿掉同名服务。
     *
     * 匹配键是插件 + 对象类型 + 名字：停池和停站是两条，起站不能把停池那条清掉。
     */
    private static void removePending(List<PendingStart> pending, String plugin, Map<String, Object> with) {
        String key = serviceKey(plugin, with);
        for (int i = pending.size() - 1; i >= 0; i--) {
            if (key.equals(serviceKey(pending.get(i).plugin, pending.get(i).with))) {
                pending.remove(i);
            }
        }
    }

    /** 待补偿名单的匹配键：同一次发布里停池、停站、停容器要分开记。 */
    private static String serviceKey(String plugin, Map<String, Object> with) {
        return (plugin == null ? "" : plugin)
                + "|" + Json.str(with.get("target"))
                + "|" + Json.str(with.get("type"))
                + "|" + Json.str(with.get("name"));
    }

    /** 上一次 file-transfer 写的备份目录与摘要，供部署记录上报使用。 */
    private String lastBackupDir = "";
    private String lastBackupSummary = "";

    /**
     * 把「怎么撤销这次部署」上报给平台。
     *
     * 光有备份目录还不够：回滚同样要换文件，发布时需要停服务的理由这里一样成立。
     * 所以顺带把这一步前后的启停配置一起记下来，回滚时原样复用，
     * 免得让人在故障现场再去想「这条线到底要不要停服务」。
     */
    private void reportDeployment(int taskId, int index, List<Object> steps,
                                  Map<String, Object> with, LogSink sink) {
        if (taskId <= 0 || agentId <= 0 || lastBackupDir == null || lastBackupDir.isEmpty()) {
            return;
        }
        try {
            Map<String, Object> payload = new HashMap<String, Object>();
            payload.put("backupDir", lastBackupDir);
            payload.put("targetDir", Json.str(with.get("targetDir")));
            List<Step> stops = findServices(steps, index, -1, "stop");
            List<Step> starts = findServices(steps, index, 1, "start");
            if (!stops.isEmpty()) {
                Step stop = stops.get(stops.size() - 1);
                payload.put("stop_with", stop.with);
                // 记下用的是哪个插件：Windows 是 iis-control，Linux 是 service-control，
                // 回滚计划照着这个还原，不能再写死成 IIS
                payload.put("stop_plugin", stop.plugin);
                payload.put("stop_steps", stepMaps(stops));
            }
            if (!starts.isEmpty()) {
                Step start = starts.get(0);
                payload.put("start_with", start.with);
                payload.put("start_plugin", start.plugin);
                payload.put("start_steps", stepMaps(starts));
            }

            Map<String, Object> body = new HashMap<String, Object>();
            body.put("step_index", index);
            body.put("kind", "file-backup");
            body.put("payload", payload);
            body.put("target", Json.str(with.get("targetDir")));
            body.put("summary", lastBackupSummary);
            api.post("/api/v1/agents/" + agentId + "/tasks/" + taskId + "/deployment", body);
        } catch (Exception e) {
            // 发布本身已经成功了，不能因为记不上账就判失败；但要说清楚后果
            sink.log("  提示：部署记录上报失败（" + e.getMessage() + "），这次发布将无法一键回滚");
        }
    }

    /** 一条服务启停步骤：插件名 + 参数。 */
    private static class Step {
        final String plugin;
        final Map<String, Object> with;
        final String name;

        Step(String plugin, Map<String, Object> with, String name) {
            this.plugin = plugin;
            this.with = with;
            this.name = name;
        }
    }

    /** 从 file-transfer 往前/往后收集连续的服务启停（停应用池+停站点要一起记）。 */
    private static List<Step> findServices(List<Object> steps, int from, int dir, String action) {
        List<Step> found = new ArrayList<Step>();
        for (int i = from + dir; i >= 0 && i < steps.size(); i += dir) {
            Map<String, Object> step = Json.obj(steps.get(i));
            String plugin = Json.str(step.get("plugin"));
            if (!isServicePlugin(plugin)) {
                break;
            }
            Map<String, Object> w = Json.obj(step.get("with"));
            String act = Json.str(w.get("action"));
            boolean match = act != null && act.equals(action);
            if (!match && "start".equals(action)
                    && ("restart".equals(act) || "reload".equals(act) || "recycle".equals(act))) {
                match = true;
            }
            // 只读状态查询夹在启停中间，不能因此把停池/停站丢掉
            if ("status".equals(act)) {
                continue;
            }
            if (!match) {
                break;
            }
            found.add(new Step(plugin, w, Json.str(step.get("name"))));
        }
        if (dir < 0) {
            Collections.reverse(found);
        }
        return found;
    }

    private static List<Map<String, Object>> stepMaps(List<Step> items) {
        List<Map<String, Object>> out = new ArrayList<Map<String, Object>>();
        for (Step item : items) {
            Map<String, Object> one = new HashMap<String, Object>();
            one.put("plugin", item.plugin);
            one.put("with", item.with);
            if (item.name != null && !item.name.isEmpty()) {
                one.put("name", item.name);
            }
            out.add(one);
        }
        return out;
    }

    private static boolean isServicePlugin(String plugin) {
        for (String p : SERVICE_PLUGINS) {
            if (p.equals(plugin)) {
                return true;
            }
        }
        return false;
    }

    private void reportStep(int taskId, int index, String plugin, String status,
                            long startedAtMs, long costMs) {
        if (taskId <= 0 || agentId <= 0) {
            return;
        }
        try {
            Map<String, Object> body = new HashMap<>();
            body.put("index", index);
            body.put("plugin", plugin == null ? "" : plugin);
            body.put("status", status);
            body.put("started_at_ms", startedAtMs);
            body.put("duration_ms", costMs);
            api.post("/api/v1/agents/" + agentId + "/tasks/" + taskId + "/step", body);
        } catch (Exception ignored) {
            // 进度上报失败不该让发布跟着失败
        }
    }

    /**
     * 任务日志里的节点标识：主机名不够认机器，带上本机 IPv4。
     * IP 拿不到时只打主机名，避免日志里出现「unknown」。
     */
    private static String nodeLabel() {
        String name = safeHostName();
        String ip = Config.detectLocalIp();
        if (ip == null || ip.isEmpty() || "unknown".equals(ip) || ip.equals(name)) {
            return name;
        }
        return name + "（" + ip + "）";
    }

    private static String safeHostName() {
        try {
            return java.net.InetAddress.getLocalHost().getHostName();
        } catch (Exception e) {
            return "unknown";
        }
    }

    // ================= 路径白名单 =================

    /**
     * 校验目标路径是否落在允许目录内。
     *
     * 用规范化后的绝对路径比对，杜绝 ..\ 和短名（PROGRA~1）绕过；
     * 没配白名单时一律拒绝，宁可发不出去也不能让生产机变成任人写盘的靶子。
     */
    static File resolveAllowed(List<String> allowPaths, String raw) throws Exception {
        if (raw == null || raw.trim().isEmpty()) {
            throw new IllegalArgumentException("没有填写目标目录");
        }
        if (allowPaths == null || allowPaths.isEmpty()) {
            throw new IllegalArgumentException(
                    "本节点没有配置允许操作的目录，出于安全考虑拒绝所有写操作。"
                            + "请在节点启动参数里加 --allow-paths \"D:\\wwwroot\"");
        }
        File target = new File(raw.trim()).getCanonicalFile();
        String targetPath = normalize(target.getPath());
        for (String allow : allowPaths) {
            File root = new File(allow).getCanonicalFile();
            String rootPath = normalize(root.getPath());
            if (targetPath.equals(rootPath) || targetPath.startsWith(rootPath + File.separator)) {
                return target;
            }
        }
        throw new IllegalArgumentException(
                "目标目录 " + target.getPath() + " 不在本节点允许的范围内 " + allowPaths
                        + "。若确需发布到这里，请在节点管理页改允许目录，等心跳生效");
    }

    /** 系统关键目录：写进去轻则污染系统，重则整台机器起不来。 */
    private static final String[] FORBIDDEN_ROOTS = {
        "c:\\windows", "c:\\program files", "c:\\program files (x86)",
        "/etc", "/bin", "/sbin", "/usr", "/boot", "/sys", "/proc", "/dev",
        "/lib", "/lib64", "/root", "/var/run", "/tmp", "/var/tmp", "/dev/shm",
    };

    /**
     * 启动时筛掉不该出现在白名单里的目录，返回可用的那部分。
     *
     * 安装脚本已经挡过一道，但那道只在「用脚本装」时有效：手工敲命令启动、
     * 或者有人改了 systemd unit，就绕过去了。白名单是这台机器最后的写盘边界，
     * 它自己不能靠「安装时没填错」来保证。
     *
     * 只筛不退出：白名单里通常有好几个目录，因为其中一个填错就让整个节点起不来，
     * 反而会逼人去掉校验。筛掉的会明确打出来。
     */
    static List<String> sanitizeAllowPaths(List<String> raw, List<String> rejected) {
        List<String> out = new ArrayList<String>();
        if (raw == null) {
            return out;
        }
        for (String p : raw) {
            if (p == null || p.trim().isEmpty()) {
                continue;
            }
            String reason = allowPathRejection(p.trim());
            if (reason == null) {
                out.add(p.trim());
            } else if (rejected != null) {
                rejected.add(p.trim() + "（" + reason + "）");
            }
        }
        return out;
    }

    /** 这个目录能不能当白名单根；能用返回 null，不能用返回原因。 */
    private static String allowPathRejection(String raw) {
        File dir;
        try {
            dir = new File(raw).getCanonicalFile();
        } catch (Exception e) {
            return "路径无法解析";
        }
        // 盘符根 / 文件系统根：等于把整台机器交出去
        if (dir.getParentFile() == null) {
            return "不能是根目录，请指到具体的站点目录";
        }
        if (isForbiddenRoot(normalize(dir.getPath()))) {
            return "是系统目录";
        }
        if (isDirectlyUnderUnixRoot(normalize(dir.getPath()))) {
            return "不能是 /data 这种整块盘，请指到具体站点目录";
        }
        return null;
    }

    /** 规范化后的路径是否落在系统关键目录里。 */
    private static boolean isForbiddenRoot(String normalizedPath) {
        for (String bad : FORBIDDEN_ROOTS) {
            File badFile = new File(bad);
            // FORBIDDEN_ROOTS 里同时放着两个平台的路径。Linux 上的 c:\windows 会被
            // 当成相对路径（当前目录下一个叫 "c:\windows" 的文件），拿它去比毫无意义；
            // 反过来 Windows 上的 /etc 也一样。只比对当前平台认得的那部分
            if (!badFile.isAbsolute()) {
                continue;
            }
            String root = normalize(badFile.getPath());
            if (normalizedPath.equals(root) || normalizedPath.startsWith(root + File.separator)) {
                return true;
            }
        }
        return false;
    }

    /**
     * 校验备份根目录。
     *
     * 备份不走 allowPaths：那份白名单管的是「哪些站点目录允许被发布覆盖」，
     * 备份是往别处写副本，两回事。强行共用只会逼人把备份塞进站点里，或者
     * 把整个盘加进白名单，两条路都更危险。
     *
     * 但有两条硬线：不能写系统目录；不能落在任何站点目录里面——备份的是
     * DLL 和 web.config，放进站点就等于把数据库连接串挂到公网上让人下载。
     */
    static File resolveBackupRoot(List<String> allowPaths, String raw) throws Exception {
        if (raw == null || raw.trim().isEmpty()) {
            throw new IllegalArgumentException("没有填写备份目录");
        }
        File dir = new File(raw.trim()).getCanonicalFile();
        String path = normalize(dir.getPath());

        if (dir.getParentFile() == null) {
            throw new IllegalArgumentException(
                    "备份目录不能是盘符根目录 " + dir.getPath() + "，请指定到具体目录，例如 D:\\backup");
        }
        if (isDirectlyUnderUnixRoot(path)) {
            throw new IllegalArgumentException(
                    "备份不能直接建在 / 下（" + dir.getPath() + "）。要用 /data/release-backup 这种，避免写到根目录");
        }
        if (isForbiddenRoot(path)) {
            throw new IllegalArgumentException(
                    "备份目录 " + dir.getPath() + " 落在系统目录里，换一个数据盘上的路径，例如 D:\\backup");
        }
        if (allowPaths != null) {
            for (String allow : allowPaths) {
                String site = normalize(new File(allow).getCanonicalFile().getPath());
                if (path.equals(site) || path.startsWith(site + File.separator)) {
                    throw new IllegalArgumentException(
                            "备份目录 " + dir.getPath() + " 在站点目录 " + allow + " 里面。"
                                    + "备份含 DLL 和 web.config，放在站点下会被 IIS 当静态文件下载，"
                                    + "请改到站点之外，例如 D:\\backup");
                }
                if (site.equals(path) || site.startsWith(path + File.separator)) {
                    throw new IllegalArgumentException(
                            "备份根 " + dir.getPath() + " 包住了站点 " + allow
                                    + "。清理备份时可能碰到站点文件，请改到与站点并列的目录");
                }
            }
        }
        return dir;
    }

    /**
     * 流水线填的备份路径必须落在安装时的备份根下面，不能指到 /tmp 让同机其它账号读走配置。
     * 老 Agent 没配 --backup-root 时不拦这一层，仍走上面的系统目录/站点校验。
     */
    static void assertInsideConfiguredBackupRoot(File dir, String configuredRoot) throws Exception {
        if (configuredRoot == null || configuredRoot.trim().isEmpty()) {
            return;
        }
        File root = new File(configuredRoot.trim()).getCanonicalFile();
        String d = normalize(dir.getCanonicalFile().getPath());
        String r = normalize(root.getPath());
        if (!d.equals(r) && !d.startsWith(r + File.separator)) {
            throw new IllegalArgumentException(
                    "备份目录必须落在本节点备份根 " + root.getPath()
                            + " 下。流水线请关掉自定义备份，或只填该根下的相对路径；换磁盘请在安装节点时设 BACKUP_ROOT，不要指到 "
                            + dir.getPath());
        }
    }

    /**
     * 解析流水线步骤里的自定义备份位置。
     *
     * 相对路径接到节点安装时的备份根下，这样表单不用再诱导人填 /var/release/backup
     * 这种十有八九会撞备份根校验的绝对路径。绝对路径仍然允许，但必须落在备份根里面。
     *
     * @param allowPaths 站点允许目录，用来拦住「备份写进站点」
     * @param raw 步骤 backupDir，调用方保证非空
     * @param configuredRoot 节点 --backup-root；相对路径依赖它
     * @return 校验过的备份根（其下还会再拼 项目/流水线/时间）
     */
    static File resolvePipelineBackupRoot(List<String> allowPaths, String raw, String configuredRoot)
            throws Exception {
        if (raw == null || raw.trim().isEmpty()) {
            throw new IllegalArgumentException("没有填写备份目录");
        }
        String trimmed = raw.trim();
        File specified = new File(trimmed);
        if (!specified.isAbsolute()) {
            if (configuredRoot == null || configuredRoot.trim().isEmpty()) {
                throw new IllegalArgumentException(
                        "相对备份路径需要节点已配置备份根。请关掉自定义备份，或在安装节点时设置 BACKUP_ROOT");
            }
            specified = new File(configuredRoot.trim(), trimmed);
        }
        File dir = resolveBackupRoot(allowPaths, specified.getPath());
        assertInsideConfiguredBackupRoot(dir, configuredRoot);
        return dir;
    }

    /**
     * 从安装目录取第一层：/data/soft/release → /data/release-backup。
     * 系统目录那一层推不出来，返回 null，安装脚本退到 /var/release/backup。
     */
    static String deriveBackupRootFromInstallDir(String installDir) {
        if (installDir == null) {
            return null;
        }
        String s = installDir.trim().replace('\\', '/');
        while (s.length() > 1 && s.endsWith("/")) {
            s = s.substring(0, s.length() - 1);
        }
        if (s.isEmpty() || s.equals("/") || !s.startsWith("/")) {
            return null;
        }
        String rest = s.substring(1);
        int slash = rest.indexOf('/');
        String first = slash < 0 ? rest : rest.substring(0, slash);
        if (first.isEmpty()) {
            return null;
        }
        String[] system = {
            ".", "..", "etc", "bin", "sbin", "usr", "boot", "sys", "proc", "dev",
            "lib", "lib64", "root", "tmp", "var",
        };
        for (int i = 0; i < system.length; i++) {
            if (first.equals(system[i])) {
                return null;
            }
        }
        String derived = "/" + first + "/release-backup";
        if (isDirectlyUnderUnixRoot(derived)) {
            return null;
        }
        return derived;
    }

    /**
     * Linux 上是否直接挂在 / 下面：/ 或 /release-backup。
     * /data/release-backup 这种两层才允许；Windows 盘符根另走 getParentFile()==null。
     */
    static boolean isDirectlyUnderUnixRoot(String normalizedPath) {
        if (normalizedPath == null || normalizedPath.isEmpty()) {
            return true;
        }
        String p = normalizedPath.replace('\\', '/');
        while (p.length() > 1 && p.endsWith("/")) {
            p = p.substring(0, p.length() - 1);
        }
        if (p.equals("/") || p.equals("/.") || p.equals("/..")) {
            return true;
        }
        if (!p.startsWith("/")) {
            return false;
        }
        return p.indexOf('/', 1) < 0;
    }

    private static String normalize(String path) {
        String p = path;
        while (p.length() > 1 && (p.endsWith(File.separator) || p.endsWith("/"))) {
            p = p.substring(0, p.length() - 1);
        }
        // Windows 路径大小写不敏感，统一成小写再比，避免 d:\ 和 D:\ 被判成不同目录
        return File.separatorChar == '\\' ? p.toLowerCase() : p;
    }

    /** 解压包内路径必须是相对路径且不含 .. 路径段，防 zip slip。 */
    static File safeChild(File root, String entryName) throws Exception {
        String name = entryName == null ? "" : entryName.replace('\\', '/');
        if (name.trim().isEmpty()) {
            throw new IllegalArgumentException("包内含空路径");
        }
        if (name.startsWith("/") || name.matches("^[A-Za-z]:.*")) {
            throw new IllegalArgumentException("包内含非法路径：" + entryName);
        }
        // 按路径段判 ..，不能用 contains("..")：那会把 jquery..min.js、foo..bak 这类
        // 合法文件名一起拒掉，而它们一被拒整次发布就失败。真正兜住越界的是下面那道
        // 规范化路径比对，这里只负责把明摆着的 ../ 提前挡下来
        for (String seg : name.split("/")) {
            if (seg.equals("..")) {
                throw new IllegalArgumentException("包内含非法路径：" + entryName);
            }
        }
        // Windows 上 "web.config:evil" 会写成备用数据流（ADS）：路径还在站点目录里，
        // 规范化比对拦不住，但落下来的东西不是正常文件，备份和回滚都看不见它
        if (File.separatorChar == '\\' && name.indexOf(':') >= 0) {
            throw new IllegalArgumentException("包内路径含冒号，Windows 上不允许：" + entryName);
        }
        File child = new File(root, name);
        String rootPath = root.getCanonicalPath();
        String childPath = child.getCanonicalPath();
        if (!childPath.equals(rootPath) && !childPath.startsWith(rootPath + File.separator)) {
            throw new IllegalArgumentException("包内路径越界：" + entryName);
        }
        return child;
    }

    static void closeQuietly(InputStream in) {
        if (in != null) {
            try {
                in.close();
            } catch (Exception ignored) {
                // ignore
            }
        }
    }
}
