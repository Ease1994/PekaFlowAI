package com.pekaflow.agent;

import java.io.File;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 构建机 Agent 入口（JDK 8，单 jar，无第三方依赖）。
 *
 * 工作流程：
 *   1. 启动时注册到平台（POST /api/v1/agents/register）
 *   2. 定时心跳（含 running_count）
 *   3. 按 --concurrency 并行领取/执行多个 Job
 *   4. 日志批量上报；取消状态短轮询
 *
 * 用法：
 *   java -jar deploy-agent.jar --server http://localhost:8080 --name my-agent \\
 *     --tags linux,maven --poll 2 --concurrency 8 --enroll-token &lt;接入凭证&gt;
 *
 * 接入凭证在平台「构建机」页面取，只有首次注册需要；注册成功后凭据落在
 * ~/.release-agent/enrolled/ 下，之后重启或升级 jar 都不用再带。
 */
public class AgentMain {

    // 平台每次重新部署都会有一两分钟连不上，两个循环加起来能刷出上百行一模一样的报错，
    // 把真正有用的信息淹掉。同样的错误只在首次和恢复时各记一行
    private static final ErrorThrottle FETCH_ERRORS = new ErrorThrottle("拉取任务");
    private static final ErrorThrottle HEARTBEAT_ERRORS = new ErrorThrottle("心跳");

    /** 折叠连续重复的错误：首次报一行，恢复时报一行带失败次数。 */
    private static final class ErrorThrottle {
        private final String what;
        private String last;
        private int count;

        ErrorThrottle(String what) {
            this.what = what;
        }

        synchronized void failed(String message) {
            String msg = message == null ? "" : message;
            if (msg.equals(last)) {
                count++;
                return;
            }
            last = msg;
            count = 1;
            System.err.println("[agent] " + what + "失败: " + msg);
        }

        synchronized void recovered() {
            if (last == null) {
                return;
            }
            System.out.println("[agent] " + what + "已恢复（其间连续失败 " + count + " 次）");
            last = null;
            count = 0;
        }
    }

    public static void main(String[] args) {
        AgentLog.install();
        Config config;
        try {
            config = Config.parse(args);
        } catch (IllegalArgumentException e) {
            System.err.println("[agent] " + e.getMessage());
            System.exit(2);
            return;
        }
        ApiClient api = new ApiClient(config);
        final AtomicInteger running = new AtomicInteger(0);
        final Semaphore slots = new Semaphore(config.concurrency);
        final ExecutorService pool = Executors.newFixedThreadPool(config.concurrency);

        final boolean isNode = "node".equals(config.role);
        System.out.println("[agent] 启动 " + config.name
                + " (role=" + config.role
                + ", os=" + config.os + ", tags=" + config.tags
                + ", concurrency=" + config.concurrency
                + ", server=" + config.serverUrl + ")");
        if (isNode) {
            System.out.println("[agent] 部署节点模式：只执行发布动作，不下载插件、不执行任意脚本");
            // 白名单是这台机器最后的写盘边界，不能靠「安装时没填错」来保证：
            // 手工启动或有人改了 systemd unit，安装脚本那道校验就绕过去了
            java.util.List<String> badPaths = new java.util.ArrayList<String>();
            config.allowPaths = NodeExecutor.sanitizeAllowPaths(config.allowPaths, badPaths);
            if (!badPaths.isEmpty()) {
                System.err.println("[agent] 已忽略不能作为白名单的目录：" + badPaths);
                System.err.println("[agent] 白名单只能指向具体的站点目录，不能是根目录或系统目录");
            }
            config.loadNodePolicy();
            System.out.println("[agent] 允许操作的目录：" + config.allowPaths);
            if (config.allowPaths.isEmpty()) {
                System.err.println("[agent] 警告：未配置允许操作的目录，所有写操作都会被拒绝。"
                        + "请在节点管理页填写，或加 --allow-paths 后重启");
            }
            if (config.backupRoot != null && !config.backupRoot.trim().isEmpty()) {
                System.out.println("[agent] 备份根目录：" + config.backupRoot);
            }
            if ("windows".equals(config.os)) {
                System.out.println("[agent] IIS 启停按站点物理路径是否在允许目录内判断。"
                        + (config.allowIis.isEmpty()
                        ? "未额外配置 --allow-iis。"
                        : "启动参数还写了 --allow-iis 额外名单：" + config.allowIis));
            } else {
                System.out.println("[agent] 允许控制的服务：" + config.allowServices);
                if (config.allowServices.isEmpty()) {
                    System.out.println("[agent] 提示：未配置 --allow-services，服务启停步骤会被拒绝。"
                            + "只传文件不停服务的话可以不配");
                }
            }
        }

        System.out.println("[agent] 版本 " + config.version + "，家目录 " + config.home);

        // 1. 注册：首次要带 --enroll-token，之后凭落盘的 token 续期
        config.loadSavedToken();
        register(api, config);

        // 平台下发的目标版本：非空表示该升级了，主循环会停止领新任务并排空
        final java.util.concurrent.atomic.AtomicReference<String> upgradeTo =
                new java.util.concurrent.atomic.AtomicReference<>(null);
        final long[] nextUpgradeAttempt = {0L};
        // 升级失败的原因，随心跳报给平台，页面上能直接看到卡在哪一步
        final java.util.concurrent.atomic.AtomicReference<String> upgradeError =
                new java.util.concurrent.atomic.AtomicReference<>(null);

        // 先接管上次没换上的 .new：Unix 上可以直接覆盖当前 jar 再退出，
        // 不必等人重跑安装脚本。成功就立刻退出，让 systemd 拉起新文件。
        if (SelfUpdater.takeoverStaged()) {
            System.exit(0);
        }
        // 接管不了才判定失败。守护进程换不上包这件事，自动重试只会反复重启，
        // 所以熔断；管理员点「重试升级」会带 force_upgrade，越过熔断再下一遍。
        String failedAttempt = SelfUpdater.consumeFailedAttempt(config.version);
        final boolean[] selfUpgradeBroken = {false};
        if (failedAttempt != null) {
            System.err.println("[agent] " + failedAttempt);
            upgradeError.set(failedAttempt);
            selfUpgradeBroken[0] = true;
        }

        // 2. 心跳线程（上报当前运行数，顺带取回升级指令）
        Thread heartbeat = new Thread(() -> {
            while (true) {
                try {
                    Map<String, Object> hb = new HashMap<>();
                    hb.put("running_count", running.get());
                    hb.put("concurrency", config.concurrency);
                    hb.put("version", config.version);
                    if (upgradeError.get() != null) {
                        hb.put("upgrade_error", upgradeError.get());
                    }
                    Object data = api.post("/api/v1/agents/" + config.agentId + "/heartbeat", hb);
                    Map<String, Object> resp = Json.obj(data);
                    // 平台轮换了鉴权 key：立刻更新内存里的 token 并落盘，ApiClient 每次请求
                    // 现取 config.token，所以下一跳/下一次拉任务就用新 key，无感知切换
                    String nt = Json.str(resp.get("new_token"));
                    if (nt != null && !nt.isEmpty() && !nt.equals(config.token)) {
                        config.token = nt;
                        config.saveToken();
                        System.out.println("[agent] 已更新轮换的接入 key");
                    }
                    applyAllowPathsPolicy(config, resp);
                    // 先把新 jar 下下来，成功了才让主循环停工。下载本身不影响在跑的任务，
                    // 这样"停了工却换不上新版"的窗口就不存在
                    boolean forced = Json.bool(resp.get("force_upgrade"));
                    // 自动重试有 5 分钟冷却，避免下载失败时每个心跳都拉一遍 jar。
                    // 管理员点「催升级」必须立刻再试：冷却是挡自动循环的，不是挡人。
                    if (Json.bool(resp.get("should_upgrade")) && upgradeTo.get() == null
                            && (!selfUpgradeBroken[0] || forced)
                            && (forced || System.currentTimeMillis() >= nextUpgradeAttempt[0])) {
                        String target = Json.str(resp.get("latest_version"));
                        System.out.println("[agent] 收到升级指令：" + config.version + " -> " + target);
                        String err = SelfUpdater.stage(api, target);
                        if (err == null) {
                            upgradeTo.set(target);
                            upgradeError.set(null);
                        } else {
                            System.err.println("[agent] 升级失败，保持当前版本继续跑：" + err);
                            upgradeError.set(err);
                            // 下载或校验失败：5 分钟内不再重试，避免心跳频率下反复拉包
                            nextUpgradeAttempt[0] = System.currentTimeMillis() + 300000;
                        }
                    }
                    HEARTBEAT_ERRORS.recovered();
                } catch (Exception e) {
                    HEARTBEAT_ERRORS.failed(e.getMessage());
                }
                try {
                    Thread.sleep(10000);
                } catch (InterruptedException ignored) {
                    return;
                }
            }
        }, "heartbeat");
        heartbeat.setDaemon(true);
        heartbeat.start();

        // 3. 拉任务主循环：有空槽才领；空闲指数退避。
        // 槽满时阻塞最多 500ms 再回头看升级标志，避免 50ms 空转把生产机 CPU 打满，
        // 也不能无限 acquire——升级排空时必须能从这里醒过来。
        long idleMs = 200;
        while (upgradeTo.get() == null) {
            try {
                if (!slots.tryAcquire(500, TimeUnit.MILLISECONDS)) {
                    continue;
                }
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                break;
            }
            Object taskData = null;
            try {
                taskData = api.get("/api/v1/agents/" + config.agentId + "/tasks");
                FETCH_ERRORS.recovered();
            } catch (Exception e) {
                FETCH_ERRORS.failed(e.getMessage());
            }

            if (taskData == null) {
                slots.release();
                sleepMs(idleMs);
                idleMs = Math.min(idleMs * 2, config.pollInterval * 1000L);
                continue;
            }
            idleMs = 200;

            final Map<String, Object> task = Json.obj(taskData);
            final int taskId = Json.integer(task.get("task_id"));
            String stage = Json.str(task.get("stage_name"));
            String job = Json.str(task.get("job_name"));
            System.out.println("[agent] 领取任务 #" + taskId + " (" + stage + "/" + job
                    + ") running=" + (running.get() + 1) + "/" + config.concurrency);

            pool.execute(() -> {
                running.incrementAndGet();
                StepRunner executor = isNode
                        ? new NodeExecutor(api, config.agentId, config.allowPaths,
                                config.allowServices, config.allowIis, config.homeDir(),
                                config.backupRoot)
                        : new TaskExecutor(config.workspaceRoot, api, config.agentId);
                LogBatcher batcher = new LogBatcher(api, config.agentId, taskId);
                Thread cancelWatcher = startCancelPoller(api, config.agentId, taskId, executor);
                boolean success = false;
                try {
                    success = executor.executeTask(task, batcher::append);
                } catch (Exception e) {
                    System.err.println("[agent] 任务 #" + taskId + " 异常: " + e.getMessage());
                    success = false;
                } finally {
                    batcher.close();
                    if (cancelWatcher != null) {
                        cancelWatcher.interrupt();
                    }
                    completeTask(api, config.agentId, taskId, success, executor.getLastSourceRef());
                    running.decrementAndGet();
                    slots.release();
                    System.out.println("[agent] 任务 #" + taskId + " 执行" + (success ? "成功" : "失败")
                            + " running=" + running.get());
                }
            });
        }

        // 4. 新 jar 已就绪，排空后退出，让守护进程完成替换并拉起
        pool.shutdown();
        try {
            // 不设上限：宁可等一个跑不完的任务，也不能把正在发布的活给砍了
            while (!pool.awaitTermination(60, TimeUnit.SECONDS)) {
                System.out.println("[agent] 还有 " + running.get() + " 个任务在跑，升级等待中…");
            }
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
        System.out.println("[agent] 退出，等待守护进程换上 " + upgradeTo.get());
        System.exit(0);
    }

    /**
     * 注册到平台。失败重试而不是立刻退出——平台重启期间 Agent 恰好启动是常态，
     * 直接退出会把恢复时间拖到守护进程的下一个巡检周期。
     */
    private static void register(ApiClient api, Config config) {
        long backoff = 3000;
        for (int attempt = 1; attempt <= 5; attempt++) {
            try {
                Map<String, Object> body = new HashMap<>();
                body.put("name", config.name);
                body.put("host", config.host);
                body.put("os", config.os);
                body.put("tags", config.tags);
                body.put("concurrency", config.concurrency);
                body.put("role", config.role);
                body.put("allow_paths", config.allowPaths);
                body.put("allow_services", config.allowServices);
                // 只在平台第一次见到这台机器时生效，之后以页面上的设置为准
                body.put("env", config.env);
                body.put("version", config.version);
                Object data = api.post("/api/v1/agents/register", body);
                config.agentId = Json.integer(Json.obj(data).get("agent_id"));
                config.token = Json.str(Json.obj(data).get("token"));
                config.saveToken();
                System.out.println("[agent] 注册成功, agent_id=" + config.agentId);
                return;
            } catch (Exception e) {
                System.err.println("[agent] 注册失败(" + attempt + "/5): " + e.getMessage());
                if (attempt < 5) {
                    sleepMs(backoff);
                    backoff = Math.min(backoff * 2, 30000);
                }
            }
        }
        if (config.enrollToken == null || config.enrollToken.isEmpty()) {
            System.err.println("[agent] 未提供接入凭证。到平台「构建机 / 节点管理」页面复制安装命令，"
                    + "或加 --enroll-token <凭证>（也可用环境变量 RELEASE_ENROLL_TOKEN）");
        }
        System.exit(1);
    }

    /**
     * 短轮询取消状态（1.5s），避免 wait-cancel 长连接占满服务端。
     */
    private static Thread startCancelPoller(ApiClient api, int agentId, int taskId, StepRunner executor) {
        Thread t = new Thread(() -> {
            while (!Thread.currentThread().isInterrupted()) {
                try {
                    Object data = api.get("/api/v1/agents/" + agentId + "/tasks/" + taskId + "/status");
                    if (data != null) {
                        Map<String, Object> obj = Json.obj(data);
                        if (Json.bool(obj.get("cancelled"))) {
                            String reason = Json.str(obj.get("reason"));
                            System.out.println("[agent] 收到取消信号（" + reason + "），将在当前步骤结束后停止后续步骤");
                            executor.notifyCancelled(reason);
                            return;
                        }
                    }
                    Thread.sleep(1500);
                } catch (InterruptedException ie) {
                    return;
                } catch (Exception e) {
                    try {
                        Thread.sleep(1500);
                    } catch (InterruptedException ie) {
                        return;
                    }
                }
            }
        }, "cancel-poll-" + taskId);
        t.setDaemon(true);
        t.start();
        return t;
    }

    private static void completeTask(ApiClient api, int agentId, int taskId, boolean success, String sourceRef) {
        try {
            Map<String, Object> body = new HashMap<>();
            body.put("success", success);
            if (sourceRef != null && !sourceRef.isEmpty()) {
                body.put("source_ref", sourceRef);
            }
            api.post("/api/v1/agents/" + agentId + "/tasks/" + taskId + "/complete", body);
        } catch (Exception e) {
            System.err.println("[agent] 完成上报失败: " + e.getMessage());
        }
    }

    /**
     * 心跳带回的允许目录：平台是真源。空列表不采用，避免旧平台或误传把现有闸门抹掉。
     */
    private static void applyAllowPathsPolicy(Config config, Map<String, Object> resp) {
        if (config == null || resp == null || !"node".equals(config.role)) {
            return;
        }
        if (!resp.containsKey("allow_paths")) {
            return;
        }
        List<Object> raw = Json.arr(resp.get("allow_paths"));
        List<String> next = new ArrayList<String>();
        for (int i = 0; i < raw.size(); i++) {
            String p = Json.str(raw.get(i)).trim();
            if (!p.isEmpty()) {
                next.add(p);
            }
        }
        List<String> rejected = new ArrayList<String>();
        List<String> cleaned = NodeExecutor.sanitizeAllowPaths(next, rejected);
        if (cleaned.isEmpty()) {
            return;
        }
        if (cleaned.equals(config.allowPaths)) {
            return;
        }
        config.allowPaths.clear();
        config.allowPaths.addAll(cleaned);
        config.saveNodePolicy();
        System.out.println("[agent] 已按平台更新允许操作的目录：" + config.allowPaths);
        if (!rejected.isEmpty()) {
            System.err.println("[agent] 平台下发的目录有部分被忽略：" + rejected);
        }
    }

    private static void sleepMs(long ms) {
        try {
            TimeUnit.MILLISECONDS.sleep(ms);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
    }
}
