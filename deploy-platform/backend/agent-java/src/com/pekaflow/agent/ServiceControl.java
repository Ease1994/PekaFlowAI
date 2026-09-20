package com.pekaflow.agent;

import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.function.BooleanSupplier;

/**
 * Linux 侧的服务启停控制，对应 Windows 的 IisControl。
 *
 * 只认两种目标：systemd 单元和 docker 容器。刻意不做「执行任意命令」——
 * 那等于把节点退化成一条 SSH 通道，allow-paths 和受限执行也就白设了。
 * 命令是固定拼装的，服务名只作为独立 argv 传入（不经过 shell），
 * 名字里带分号或反引号也拼不出第二条命令。
 *
 * 能操作哪些服务由节点启动时的 --allow-services 声明，和 --allow-paths 一样是
 * 画在本机的线：平台下发什么都越不过去。机器上还有第二道 sudoers 白名单兜底，
 * 那道线连 Agent 自己被改了参数也绕不开。
 */
public class ServiceControl {

    private final List<String> allowServices;

    /**
     * 本次是否真正发出了 stop。已经是停止态的空操作不算，避免补偿启动把本来停着的服务拉起来。
     */
    private boolean issuedStop;

    /** 本次是否真正发出了 start / restart / reload。 */
    private boolean issuedStart;

    public ServiceControl(List<String> allowServices) {
        this.allowServices = allowServices == null ? new ArrayList<String>() : allowServices;
    }

    /** 本次 run 是否真正执行了 stop。供失败/取消时补偿启动使用。 */
    public boolean issuedStop() {
        return issuedStop;
    }

    /** 本次 run 是否真正执行了会让服务跑起来的动作。 */
    public boolean issuedStart() {
        return issuedStart;
    }

    public boolean run(Map<String, Object> with, LogSink sink) throws Exception {
        return run(with, sink, null);
    }

    /** 服务名允许的字符：systemd 的模板实例用 @，单元后缀用 .，容器名用 _ 和 -。 */
    static final String NAME_PATTERN = "^[A-Za-z0-9][A-Za-z0-9._@-]*$";

    /** 查容器是否在跑的格式串。安装脚本要把同样的字面量写进 sudoers，两边必须一致。 */
    static final String DOCKER_RUNNING_FMT = "{{.State.Running}}";

    /** 等服务变成目标状态的最长时间。 */
    static final int MAX_WAIT_SECONDS = 600;

    /**
     * 执行一次 Linux 服务启停。
     *
     * @param cancelled 等待状态变化时可中断；补偿启动时传 null，取消信号不能把恢复动作掐掉
     */
    public boolean run(Map<String, Object> with, LogSink sink, BooleanSupplier cancelled) throws Exception {
        issuedStop = false;
        issuedStart = false;
        String type = str(with.get("type"), "systemd").toLowerCase();
        String name = str(with.get("name"), "");
        String action = str(with.get("action"), "restart").toLowerCase();
        boolean ignoreMissing = Json.bool(with.get("ignoreMissing"));
        int waitSeconds = Json.integer(with.get("waitSeconds"));
        if (waitSeconds <= 0) {
            waitSeconds = 30;
        }
        // 上限 10 分钟。不封的话流水线里填个 999999 就能让这个节点等上十几天：
        // 并发是 1，槽位一直占着，这台机器等于从队列里消失了
        if (waitSeconds > MAX_WAIT_SECONDS) {
            sink.log("  等待时长 " + waitSeconds + " 秒过长，按上限 " + MAX_WAIT_SECONDS + " 秒处理");
            waitSeconds = MAX_WAIT_SECONDS;
        }

        if (File.separatorChar == '\\') {
            sink.log("  这台是 Windows 节点，请改用「IIS 启停控制」步骤");
            return false;
        }
        if (name.isEmpty()) {
            sink.log("  没有填写服务名 / 容器名");
            return false;
        }
        if (!name.matches(NAME_PATTERN)) {
            sink.log("  服务名「" + name + "」含非法字符，只允许字母数字和 . _ - @");
            return false;
        }
        if (!"systemd".equals(type) && !"docker".equals(type)) {
            sink.log("  不支持的类型「" + type + "」，只能是 systemd 或 docker");
            return false;
        }
        if (!allowed(type, name)) {
            sink.log("  服务「" + type + ":" + name + "」不在本节点允许控制的范围内 " + allowServices
                    + "。若确需控制它，请修改节点启动参数的 --allow-services 并重装（会同步更新 sudoers 白名单）");
            return false;
        }

        String label = ("docker".equals(type) ? "容器 " : "服务 ") + name;
        Boolean running = queryRunning(type, name);
        if (running == null) {
            if (ignoreMissing) {
                sink.log("  " + label + " 不存在，按配置跳过");
                return true;
            }
            sink.log("  " + label + " 不存在。请核对名称"
                    + ("systemd".equals(type) ? "（systemctl list-units 里的单元名）" : "（docker ps -a 里的容器名）"));
            return false;
        }
        sink.log("  " + label + " 当前状态：" + (running ? "运行中" : "已停止"));

        if ("status".equals(action)) {
            return true;
        }

        boolean want;
        if ("start".equals(action)) {
            want = true;
        } else if ("stop".equals(action)) {
            want = false;
        } else if ("restart".equals(action) || "reload".equals(action)) {
            want = true;
        } else {
            sink.log("  不支持的动作「" + action + "」，可用：start / stop / restart / reload / status");
            return false;
        }

        // 已经是目标状态就别再操作一次：停一个停着的服务，systemctl 返回 0，
        // docker stop 也返回 0，但白折腾一轮；而 start 一个跑着的容器 docker 会报错
        if (("start".equals(action) || "stop".equals(action)) && running.booleanValue() == want) {
            sink.log("  已经是「" + (want ? "运行中" : "已停止") + "」，无需操作");
            return true;
        }
        // 重载一个没跑起来的服务，systemctl reload 会直接失败。想要的结果本来就是
        // 「服务能对外提供」，那就改成启动，不必让发布因为这个停下来
        String verb = action;
        if ("reload".equals(action)) {
            if ("docker".equals(type)) {
                sink.log("  容器不支持 reload，改用 restart");
                verb = "restart";
            } else if (!running) {
                sink.log("  " + label + " 当前没在跑，reload 改为 start");
                verb = "start";
            }
        }
        if ("restart".equals(verb) && "docker".equals(type) && !running) {
            // docker restart 一个已停止的容器是可以的，但语义上 start 更准确，日志也更好读
            verb = "start";
        }

        // 一律用补齐后的单元名（nginx → nginx.service）。sudoers 匹配的是完整命令行，
        // 白名单里写的是哪种写法、这里就必须发出哪种，否则一个能跑一个被拒
        String[] cmd = "systemd".equals(type)
                ? sudo(systemctl(), verb, unitName(name))
                : sudo(docker(), verb, name);
        sink.log("  执行：" + join(cmd));
        if ("stop".equals(verb)) {
            issuedStop = true;
        } else {
            issuedStart = true;
        }
        // systemctl restart 会一直阻塞到单元起停完成，单元自己的 TimeoutStopSec
        // 默认就有 90 秒，所以这里给的余量要比状态轮询宽
        Result r = exec(cmd, waitSeconds + 120);
        if (r.code != 0) {
            // 查状态到执行之间有空档，别人（另一次发布、运维手动操作、进程自己崩了重拉）
            // 可能已经把它弄成目标状态了。报错文案跟着语言和版本走，靠关键字匹配不可靠，
            // 直接再查一次：只要结果就是想要的，这条命令失不失败都无所谓
            Boolean after = queryRunning(type, name);
            if (after != null && after.booleanValue() == want) {
                sink.log("  " + label + " 已经是目标状态，忽略上面的报错");
                return true;
            }
            sink.log("  执行失败（退出码 " + r.code + "）：" + r.output.trim());
            hintOnFailure(r.output, type, name, sink);
            return false;
        }

        // 等状态真的变过去：systemctl 返回成功不代表进程已经退干净，
        // 这时候就去替换 jar / war 会撞上文件占用
        long deadline = System.currentTimeMillis() + waitSeconds * 1000L;
        while (System.currentTimeMillis() < deadline) {
            if (cancelled != null && cancelled.getAsBoolean()) {
                sink.log("  收到取消，停止等待 " + label + " 状态变化");
                return false;
            }
            Boolean now = queryRunning(type, name);
            if (now != null && now.booleanValue() == want) {
                sink.log("  " + label + " 已" + (want ? "启动" : "停止"));
                return true;
            }
            Thread.sleep(500);
        }
        sink.log("  等待 " + waitSeconds + " 秒后 " + label + " 仍未变为「"
                + (want ? "运行中" : "已停止") + "」，请到服务器上确认");
        return false;
    }

    /** sudo 失败时给出可操作的下一步，别让人对着「Permission denied」发呆。 */
    private void hintOnFailure(String output, String type, String name, LogSink sink) {
        if (isSudoRejection(output)) {
            sink.log("  节点 Agent 是以普通用户跑的，靠 sudoers 白名单才能操作服务。"
                    + "请确认 /etc/sudoers.d/release-node 里有这一条，或重跑安装脚本并在 "
                    + "ALLOW_SERVICES 里带上 " + type + ":" + name);
        }
    }

    /** 这次失败是不是 sudo 挡下来的。文案跟着系统语言走，只能靠这几个稳定的英文关键字。 */
    private static boolean isSudoRejection(String output) {
        String low = output == null ? "" : output.toLowerCase();
        return low.contains("a password is required")
                || low.contains("sudo:")
                || low.contains("not allowed")
                || low.contains("permission denied");
    }

    private boolean allowed(String type, String name) {
        return allowed(allowServices, type, name);
    }

    /** 服务是否在允许范围内。白名单写 `systemd:nginx`，省略类型则按 systemd 解释。 */
    static boolean allowed(List<String> allowServices, String type, String name) {
        if (allowServices == null || allowServices.isEmpty()) {
            return false;
        }
        for (String raw : allowServices) {
            String item = raw == null ? "" : raw.trim();
            if (item.isEmpty()) {
                continue;
            }
            String t = "systemd";
            String n = item;
            int idx = item.indexOf(':');
            if (idx > 0) {
                t = item.substring(0, idx).trim().toLowerCase();
                n = item.substring(idx + 1).trim();
            }
            // nginx 和 nginx.service 是同一个东西，别让人因为写法不同被拒
            if (t.equals(type) && unitEquals(n, name)) {
                return true;
            }
        }
        return false;
    }

    private static boolean unitEquals(String a, String b) {
        return stripUnit(a).equals(stripUnit(b));
    }

    private static String stripUnit(String s) {
        return s.endsWith(".service") ? s.substring(0, s.length() - ".service".length()) : s;
    }

    /**
     * 查运行状态；对象不存在返回 null。
     *
     * systemctl is-active 对「不存在」和「已停止」都返回非 0，区分不出来，
     * 所以先用 list-unit-files 确认单元存在。
     */
    private Boolean queryRunning(String type, String name) throws Exception {
        if ("docker".equals(type)) {
            // 查状态也走 sudo：Agent 跑在普通用户下，不在 docker 组，直连
            // /var/run/docker.sock 会被拒，那样每个容器都会被误判成「不存在」
            Result r = exec(sudo(docker(), "inspect", "-f", DOCKER_RUNNING_FMT, name));
            if (r.code != 0) {
                // sudo 没放行和容器不存在，docker 都是非 0 退出。当成「不存在」报出去
                // 会把人引到「是不是名字写错了」上，实际问题在 sudoers，差很远
                if (isSudoRejection(r.output)) {
                    throw new IllegalStateException(
                            "查容器状态被 sudo 拒绝：" + r.output.trim()
                            + "。请确认 /etc/sudoers.d/release-node 里放行了 docker inspect "
                            + name + "，或重跑安装脚本并在 ALLOW_SERVICES 里带上 docker:" + name);
                }
                return null;  // 容器不存在
            }
            return Boolean.valueOf(r.output.trim().contains("true"));
        }
        Result exists = exec(new String[]{systemctl(), "list-unit-files", "--no-legend", unitName(name)});
        if (exists.code != 0 || exists.output.trim().isEmpty()) {
            // 有些发行版上 list-unit-files 不列出 systemd 生成的单元（比如挂载和 sysv 兼容），
            // 再用 is-active 兜一次：能问出确定答案就说明单元是存在的
            Result act = exec(new String[]{systemctl(), "is-active", unitName(name)});
            String s = act.output.trim();
            if (s.equals("active") || s.equals("activating")) {
                return Boolean.TRUE;
            }
            if (s.equals("inactive") || s.equals("failed") || s.equals("deactivating")) {
                return Boolean.FALSE;
            }
            return null;
        }
        Result act = exec(new String[]{systemctl(), "is-active", unitName(name)});
        String s = act.output.trim();
        return Boolean.valueOf(s.equals("active") || s.equals("activating"));
    }

    private static String unitName(String name) {
        return name.contains(".") ? name : name + ".service";
    }

    /**
     * 加 sudo -n：Agent 以普通用户运行，靠 sudoers 白名单拿到这几条命令的权限。
     *
     * -n 是关键——没配 sudoers 时直接失败退出，而不是挂在那儿等人输密码。
     * 节点是无人值守的，等密码就是永久卡住一次发布。
     */
    private static String[] sudo(String bin, String... args) {
        List<String> cmd = new ArrayList<String>();
        // root 身份跑的时候没必要绕 sudo，有些精简镜像里根本没装
        if (!isRoot()) {
            cmd.add("sudo");
            cmd.add("-n");
        }
        cmd.add(bin);
        for (String a : args) {
            cmd.add(a);
        }
        return cmd.toArray(new String[0]);
    }

    private static boolean isRoot() {
        // 没有现成 API 拿 uid，用 root 的家目录判断足够了：这里只影响加不加 sudo，
        // 判断错了最多是多绕一层 sudo 或少绕一层，两种情况都还有 sudoers 兜底
        String user = System.getProperty("user.name", "");
        return "root".equals(user);
    }

    private static String systemctl() {
        return firstExisting("/bin/systemctl", "/usr/bin/systemctl", "systemctl");
    }

    private static String docker() {
        return firstExisting("/usr/bin/docker", "/bin/docker", "/usr/local/bin/docker", "docker");
    }

    /** 用绝对路径调用，避免 PATH 被改动后调到别的同名程序上。 */
    private static String firstExisting(String... candidates) {
        for (String c : candidates) {
            if (c.startsWith("/") && new File(c).isFile()) {
                return c;
            }
        }
        return candidates[candidates.length - 1];
    }

    private static String join(String[] cmd) {
        StringBuilder sb = new StringBuilder();
        for (String c : cmd) {
            if (sb.length() > 0) {
                sb.append(' ');
            }
            sb.append(c);
        }
        return sb.toString();
    }

    /** 查状态类命令的超时：都是本地读取，正常几十毫秒，卡住说明系统本身有问题。 */
    private static final int QUERY_TIMEOUT_SECONDS = 30;

    /** 单条命令输出上限，防止某个服务往 stderr 狂喷把 Agent 内存吃掉。 */
    private static final int MAX_OUTPUT_CHARS = 64 * 1024;

    private static Result exec(String[] cmd) throws Exception {
        return exec(cmd, QUERY_TIMEOUT_SECONDS);
    }

    /**
     * 执行一条命令，超时就强杀。
     *
     * 超时这道保险是给节点自己上的，不是为了命令跑得快。节点并发是 1，
     * 这里一旦无限等下去，槽位永远放不出来，而心跳线程还在跑——平台上这台节点
     * 显示「在线」，实际上从此不接任何任务，且没法取消（取消只在步骤之间生效）。
     * 那种故障要人登机器重启 Agent 才能恢复，比一条命令失败严重得多。
     */
    private static Result exec(String[] cmd, int timeoutSeconds) throws Exception {
        ProcessBuilder pb = new ProcessBuilder(cmd);
        pb.redirectErrorStream(true);
        final Process p = pb.start();
        // 关掉 stdin：命令万一等输入，无人值守的节点会一直等下去。sudo 已经带 -n，
        // 但换个发行版、换个 systemctl 版本会不会弹别的交互，赌不起
        try {
            p.getOutputStream().close();
        } catch (Exception ignored) {
            // ignore
        }

        final java.util.concurrent.atomic.AtomicBoolean timedOut =
                new java.util.concurrent.atomic.AtomicBoolean(false);
        final long limitMs = Math.max(1, timeoutSeconds) * 1000L;
        Thread watchdog = new Thread(new Runnable() {
            public void run() {
                try {
                    if (!p.waitFor(limitMs, java.util.concurrent.TimeUnit.MILLISECONDS)) {
                        timedOut.set(true);
                        // 杀掉后下面的 readLine 会因为管道 EOF 返回，不会卡在读上
                        p.destroyForcibly();
                    }
                } catch (InterruptedException ignored) {
                    // 正常结束时被叫醒
                }
            }
        }, "rp-svc-exec-watchdog");
        watchdog.setDaemon(true);
        watchdog.start();

        StringBuilder sb = new StringBuilder();
        BufferedReader reader = new BufferedReader(
                new InputStreamReader(p.getInputStream(), "UTF-8"));
        try {
            String line;
            while ((line = reader.readLine()) != null) {
                // 超上限后继续读、但不再往内存里堆：不读会把管道堵满，
                // 进程卡在写 stdout 上退不掉，等于又回到卡死
                if (sb.length() < MAX_OUTPUT_CHARS) {
                    sb.append(line).append('\n');
                }
            }
        } finally {
            reader.close();
        }
        int code = p.waitFor();
        watchdog.interrupt();
        if (timedOut.get()) {
            return new Result(-1, sb.toString()
                    + "命令执行超过 " + timeoutSeconds + " 秒未返回，已强制结束。"
                    + "请到服务器上确认该服务当前状态\n");
        }
        return new Result(code, sb.toString());
    }

    private static String str(Object v, String def) {
        String s = Json.str(v);
        return (s == null || s.trim().isEmpty()) ? def : s.trim();
    }

    private static class Result {
        final int code;
        final String output;

        Result(int code, String output) {
            this.code = code;
            this.output = output == null ? "" : output;
        }
    }
}
