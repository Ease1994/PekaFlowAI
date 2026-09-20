package com.pekaflow.agent;

import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.function.BooleanSupplier;

/**
 * IIS 应用池 / 站点的启停控制。
 *
 * 用 appcmd.exe 而不是 PowerShell 的 WebAdministration 模块：appcmd 从 IIS 7 就在，
 * Windows Server 2012 上不用装任何东西也不用改执行策略，行为也更可预期。
 *
 * 命令行是固定拼装的，参数只作为独立 argv 传入（不经过 cmd /C），
 * 所以应用池名里就算带引号或 &amp; 也拼不出第二条命令。
 */
public class IisControl {

    /** 等应用池/站点变成目标状态的最长时间。 */
    static final int MAX_WAIT_SECONDS = 600;

    /**
     * 节点允许操作的目录。IIS 启停按站点/应用池的物理路径是否落在这里面判断，
     * 不再单独维护一份应用池名单。
     */
    private final List<String> allowPaths;

    /**
     * 可选的额外名字名单（启动参数 --allow-iis）。空 = 只按路径判断。
     * 老节点若已经写了这份名单，仍作为额外收紧，避免突然放宽。
     */
    private final List<String> allowIis;

    /**
     * 本次是否真正发出了 stop。已经是停止态的空操作不算：
     * 补偿启动只该拉起「被这次发布停下」的服务，不能把本来就停着的站点拉起来。
     */
    private boolean issuedStop;

    /** 本次是否真正发出了 start / recycle。成功后 NodeExecutor 会从待补偿列表里拿掉。 */
    private boolean issuedStart;

    private static final String APPCMD = System.getenv("SystemRoot") == null
            ? "C:\\Windows\\System32\\inetsrv\\appcmd.exe"
            : System.getenv("SystemRoot") + "\\System32\\inetsrv\\appcmd.exe";

    public IisControl(List<String> allowPaths, List<String> allowIis) {
        this.allowPaths = allowPaths == null ? new ArrayList<String>() : allowPaths;
        this.allowIis = allowIis == null ? new ArrayList<String>() : allowIis;
    }

    /** 本次 run 是否真正执行了 stop。供失败/取消时补偿启动使用。 */
    public boolean issuedStop() {
        return issuedStop;
    }

    /** 本次 run 是否真正执行了 start / recycle。 */
    public boolean issuedStart() {
        return issuedStart;
    }

    public boolean run(Map<String, Object> with, LogSink sink) throws Exception {
        return run(with, sink, null);
    }

    /**
     * 执行一次 IIS 启停。
     *
     * @param cancelled 等待状态变化时可中断；补偿启动时传 null，取消信号不能把恢复动作掐掉
     */
    public boolean run(Map<String, Object> with, LogSink sink, BooleanSupplier cancelled) throws Exception {
        issuedStop = false;
        issuedStart = false;
        String action = str(with.get("action"), "stop");
        String target = str(with.get("target"), "apppool");
        String name = str(with.get("name"), "");
        boolean ignoreMissing = Json.bool(with.get("ignoreMissing"));
        int waitSeconds = Json.integer(with.get("waitSeconds"));
        if (waitSeconds <= 0) {
            waitSeconds = 15;
        }
        // 上限 10 分钟。不封的话流水线里填个 999999 就能让这个节点等上十几天：
        // 并发是 1，槽位一直占着，这台机器等于从队列里消失了
        if (waitSeconds > MAX_WAIT_SECONDS) {
            sink.log("  等待时长 " + waitSeconds + " 秒过长，按上限 " + MAX_WAIT_SECONDS + " 秒处理");
            waitSeconds = MAX_WAIT_SECONDS;
        }

        if (name.trim().isEmpty()) {
            sink.log("  没有填写应用池 / 站点名称");
            return false;
        }
        // 参数是作为独立 argv 传给 appcmd 的，拼不出第二条命令，所以这里不是防注入，
        // 而是防「名字里混进控制字符或换行」这种一看日志就懵的情况。站点名可能是
        // 域名形式（o2o.example.com），所以点和横杠要放行
        if (!name.matches("^[A-Za-z0-9][A-Za-z0-9._\\- ]*$")) {
            sink.log("  名称「" + name + "」含非法字符。应用池/站点名只允许字母数字和 . _ - 及空格");
            return false;
        }
        if (allowPaths == null || allowPaths.isEmpty()) {
            sink.log("  未配置允许操作的目录，拒绝全部 IIS 控制。请在节点管理页补上目录，或加 --allow-paths 后重启");
            return false;
        }
        if (!nameAllowed(allowIis, target, name)) {
            sink.log("  「" + name + "」不在本节点 --allow-iis 额外名单 " + allowIis
                    + "。这份名单是启动参数里的额外收紧，改名单请改节点页的允许目录，或拿掉启动参数里的 --allow-iis");
            return false;
        }
        if (!new File(APPCMD).isFile()) {
            sink.log("  本机找不到 appcmd.exe（" + APPCMD + "），请确认这台机器装了 IIS");
            return false;
        }

        String object = "site".equals(target) ? "site" : "apppool";
        String label = ("site".equals(target) ? "站点" : "应用池") + " " + name;

        String current = queryState(object, name);
        if (current == null) {
            if (ignoreMissing) {
                sink.log("  " + label + " 不存在，按配置跳过");
                return true;
            }
            sink.log("  " + label + " 不存在。请核对 IIS 里的名称（大小写要一致）");
            return false;
        }
        sink.log("  " + label + " 当前状态：" + current);

        // 对象存在就必须能证明物理路径在白名单内。查不到、查失败、有一个路径在外面，一律拒绝。
        List<String> physical = listPhysicalPaths(object, name, sink);
        if (physical.isEmpty()) {
            sink.log("  无法确认 " + label + " 的物理路径，拒绝启停（失败关闭）。"
                    + "请到服务器上用 appcmd 核对该对象是否指向允许目录内的站点");
            return false;
        }
        for (int i = 0; i < physical.size(); i++) {
            String rawPath = physical.get(i);
            if (!pathAllowed(allowPaths, rawPath)) {
                sink.log("  「" + rawPath + "」不在节点允许操作的目录 " + allowPaths + " 内，拒绝控制 " + label);
                return false;
            }
        }
        sink.log("  已核对 " + physical.size() + " 个物理路径，均在允许目录内");

        if ("status".equals(action)) {
            return true;
        }

        String want;
        String verb;
        if ("start".equals(action)) {
            verb = "start";
            want = "Started";
        } else if ("recycle".equals(action)) {
            if (!"apppool".equals(object)) {
                sink.log("  回收只对应用池有效，站点请用停止/启动");
                return false;
            }
            verb = "recycle";
            want = "Started";
        } else {
            verb = "stop";
            want = "Stopped";
        }

        if (!"recycle".equals(verb) && want.equalsIgnoreCase(current)) {
            sink.log("  已经是「" + want + "」，无需操作");
            return true;
        }
        // 回收一个停着的池 appcmd 会直接报错。想要的结果本来就是「池能服务」，
        // 那就改成启动，不必让发布因为这个停下来
        if ("recycle".equals(verb) && "Stopped".equalsIgnoreCase(current)) {
            sink.log("  " + label + " 当前是停止的，回收改为启动");
            verb = "start";
        }

        sink.log("  执行：appcmd " + verb + " " + object + " \"" + name + "\"");
        if ("stop".equals(verb)) {
            issuedStop = true;
        } else {
            issuedStart = true;
        }
        Result r = exec(new String[]{APPCMD, verb, object, "/" + object + ".name:" + name},
                ACTION_TIMEOUT_SECONDS);
        if (r.code != 0) {
            // 查状态到执行之间有空档，别人（另一次发布、运维手动、IIS 自己回收）
            // 可能已经把它弄成目标状态了，appcmd 这时会报「已经启动/已经停止」。
            // 报错文案跟着系统语言走，靠关键字匹配不可靠，直接再查一次状态：
            // 只要结果就是想要的，这条命令失不失败都无所谓
            String after = queryState(object, name);
            if (after != null && want.equalsIgnoreCase(after)) {
                sink.log("  " + label + " 已经是「" + want + "」，忽略 appcmd 的报错");
                return true;
            }
            sink.log("  appcmd 执行失败（退出码 " + r.code + "）：" + r.output.trim());
            if (r.output.contains("拒绝访问") || r.output.toLowerCase().contains("access is denied")) {
                sink.log("  节点 Agent 需要以管理员身份运行才能操作 IIS");
            }
            return false;
        }

        // 等状态真的变过去：appcmd 返回成功不代表工作进程已经退干净，
        // 这时候就去替换 dll 会撞上文件占用
        long deadline = System.currentTimeMillis() + waitSeconds * 1000L;
        while (System.currentTimeMillis() < deadline) {
            if (cancelled != null && cancelled.getAsBoolean()) {
                sink.log("  收到取消，停止等待 " + label + " 状态变化");
                return false;
            }
            String now = queryState(object, name);
            if (now != null && want.equalsIgnoreCase(now)) {
                sink.log("  " + label + " 已" + ("Started".equals(want) ? "启动" : "停止"));
                return true;
            }
            Thread.sleep(500);
        }
        sink.log("  等待 " + waitSeconds + " 秒后 " + label + " 仍未变为「" + want + "」，请到服务器上确认");
        return false;
    }

    /** 查询状态，对象不存在返回 null。 */
    private String queryState(String object, String name) throws Exception {
        Result r = exec(new String[]{APPCMD, "list", object, name});
        if (r.code != 0 || r.output.trim().isEmpty()) {
            return null;
        }
        // 输出形如：APPPOOL "DefaultAppPool" (MgdVersion:v4.0,MgdMode:Integrated,state:Started)
        int idx = r.output.indexOf("state:");
        if (idx < 0) {
            return null;
        }
        String rest = r.output.substring(idx + "state:".length());
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < rest.length(); i++) {
            char c = rest.charAt(i);
            if (!Character.isLetter(c)) {
                break;
            }
            sb.append(c);
        }
        String state = sb.toString();
        return state.isEmpty() ? null : state;
    }

    /** 查状态类命令的超时。appcmd list 正常是毫秒级，卡住说明 IIS 本身不正常。 */
    private static final int QUERY_TIMEOUT_SECONDS = 30;

    /** 启停类命令的超时。appcmd 自己不等工作进程退干净，正常也是秒级返回。 */
    private static final int ACTION_TIMEOUT_SECONDS = 120;

    /** 单条命令输出上限，防止 appcmd 异常刷屏把 Agent 内存吃掉。 */
    private static final int MAX_OUTPUT_CHARS = 64 * 1024;

    private static Result exec(String[] cmd) throws Exception {
        return exec(cmd, QUERY_TIMEOUT_SECONDS);
    }

    /**
     * 执行一条命令，超时就强杀。
     *
     * 超时这道保险是给节点自己上的。节点并发是 1，这里一旦无限等下去，槽位永远
     * 放不出来，而心跳线程还在跑——平台上这台节点显示「在线」，实际上从此不接任何
     * 任务，且没法取消（取消只在步骤之间生效）。那种故障要人登机器重启 Agent
     * 才能恢复，比一条 appcmd 失败严重得多。
     */
    private static Result exec(String[] cmd, int timeoutSeconds) throws Exception {
        ProcessBuilder pb = new ProcessBuilder(cmd);
        pb.redirectErrorStream(true);
        final Process p = pb.start();
        // 关掉 stdin：appcmd 万一弹交互确认，无人值守的节点会一直等下去
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
        }, "rp-iis-exec-watchdog");
        watchdog.setDaemon(true);
        watchdog.start();

        StringBuilder sb = new StringBuilder();
        BufferedReader reader = new BufferedReader(
                new InputStreamReader(p.getInputStream(), consoleCharset()));
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
                    + "请到服务器上确认该应用池/站点当前状态\n");
        }
        return new Result(code, sb.toString());
    }

    /** 中文 Windows 控制台默认 GBK，用 UTF-8 读会把错误信息读成乱码。 */
    private static String consoleCharset() {
        String enc = System.getProperty("sun.jnu.encoding");
        if (enc == null || enc.trim().isEmpty()) {
            enc = System.getProperty("file.encoding", "UTF-8");
        }
        try {
            java.nio.charset.Charset.forName(enc);
            return enc;
        } catch (Exception e) {
            return "UTF-8";
        }
    }

    /** 单次最多核对的应用数。一个池挂几十个站本身就不该整池停。 */
    private static final int MAX_APPS_TO_CHECK = 20;

    /**
     * 查出这个池/站点实际落在磁盘上的路径。
     *
     * 一条 appcmd 把应用配置（含虚拟目录）导出来再解析 physicalPath。
     * 查失败、输出被截断、一条路径都没有，都返回空列表，调用方失败关闭，不猜测。
     */
    private List<String> listPhysicalPaths(String object, String name, LogSink sink) throws Exception {
        String filter = "site".equals(object)
                ? "/site.name:" + name
                : "/apppool.name:" + name;
        Result cfg = exec(new String[]{APPCMD, "list", "app", filter, "/config:*", "/xml"});
        if (cfg.code != 0) {
            sink.log("  查询应用配置失败（退出码 " + cfg.code + "）：" + cfg.output.trim());
            return new ArrayList<String>();
        }
        if (cfg.output.length() >= MAX_OUTPUT_CHARS) {
            sink.log("  应用配置输出过长，拒绝启停，以免漏核白名单外的路径");
            return new ArrayList<String>();
        }
        int apps = countAppsInConfig(cfg.output);
        if (apps > MAX_APPS_TO_CHECK) {
            sink.log("  " + object + " 「" + name + "」挂了 " + apps + " 个应用，超过 "
                    + MAX_APPS_TO_CHECK + " 个，拒绝整对象启停，以免误伤白名单外的站点");
            return new ArrayList<String>();
        }
        List<String> paths = parsePhysicalPathsFromConfig(cfg.output);
        if (!paths.isEmpty()) {
            return paths;
        }
        // 个别 IIS 的 /config 不带虚拟目录。站点可以再查一次 vdir；应用池没有对应过滤，不再猜。
        if ("site".equals(object)) {
            Result vdirs = exec(new String[]{APPCMD, "list", "vdir", filter, "/text:physicalPath"});
            if (vdirs.code != 0) {
                sink.log("  查询站点虚拟目录失败（退出码 " + vdirs.code + "）：" + vdirs.output.trim());
                return new ArrayList<String>();
            }
            if (vdirs.output.length() >= MAX_OUTPUT_CHARS) {
                sink.log("  虚拟目录输出过长，拒绝启停，以免漏核路径");
                return new ArrayList<String>();
            }
            return parsePathLines(vdirs.output);
        }
        sink.log("  应用配置里没有 physicalPath");
        return new ArrayList<String>();
    }

    /**
     * appcmd /text:physicalPath 一行一个路径。空行丢掉。
     */
    static List<String> parsePathLines(String output) {
        List<String> out = new ArrayList<String>();
        if (output == null) {
            return out;
        }
        String[] lines = output.split("\n");
        for (int i = 0; i < lines.length; i++) {
            String line = lines[i].trim();
            if (!line.isEmpty()) {
                out.add(line);
            }
        }
        return out;
    }

    /**
     * 从 appcmd /config /xml 里抽出所有 physicalPath。
     * 应用根和虚拟目录都是这个属性，漏掉任意一个都会让路径核不全。
     */
    static List<String> parsePhysicalPathsFromConfig(String xml) {
        List<String> out = new ArrayList<String>();
        if (xml == null || xml.isEmpty()) {
            return out;
        }
        collectQuotedAttr(xml, "physicalPath=\"", '"', out);
        collectQuotedAttr(xml, "physicalPath='", '\'', out);
        return out;
    }

    /** 统计配置里的应用个数，用来挡住「一个池挂了整机站点」。 */
    static int countAppsInConfig(String xml) {
        int n = countIgnoreCase(xml, "<application");
        if (n > 0) {
            return n;
        }
        return countIgnoreCase(xml, "<APP ");
    }

    private static void collectQuotedAttr(String xml, String needle, char quote, List<String> out) {
        int from = 0;
        while (true) {
            int i = indexOfIgnoreCase(xml, needle, from);
            if (i < 0) {
                return;
            }
            int start = i + needle.length();
            int end = xml.indexOf(quote, start);
            if (end < 0) {
                return;
            }
            String p = unescapeXml(xml.substring(start, end)).trim();
            if (!p.isEmpty() && !out.contains(p)) {
                out.add(p);
            }
            from = end + 1;
        }
    }

    private static int countIgnoreCase(String xml, String token) {
        if (xml == null || token == null || token.isEmpty()) {
            return 0;
        }
        int n = 0;
        int from = 0;
        while (true) {
            int i = indexOfIgnoreCase(xml, token, from);
            if (i < 0) {
                return n;
            }
            n++;
            from = i + token.length();
        }
    }

    private static String unescapeXml(String s) {
        return s.replace("&quot;", "\"")
                .replace("&apos;", "'")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&amp;", "&");
    }

    /**
     * 物理路径是否落在允许目录内。查不到、含未展开的 %VAR%、规范化失败，都算不允许。
     */
    static boolean pathAllowed(List<String> allowPaths, String raw) {
        String expanded = expandWinEnv(raw);
        if (expanded == null || expanded.trim().isEmpty()) {
            return false;
        }
        try {
            NodeExecutor.resolveAllowed(allowPaths, expanded);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    /**
     * 展开 IIS 常见的 %SystemDrive% / %SystemRoot% / %windir%。
     * 展开后还留着 % 就返回 null，避免把未解析变量当成合法路径。
     */
    static String expandWinEnv(String raw) {
        if (raw == null) {
            return null;
        }
        String s = raw.trim();
        if (s.isEmpty()) {
            return null;
        }
        s = replaceEnv(s, "SystemDrive");
        s = replaceEnv(s, "SystemRoot");
        s = replaceEnv(s, "windir");
        if (s.indexOf('%') >= 0) {
            return null;
        }
        return s;
    }

    private static String replaceEnv(String s, String name) {
        String token = "%" + name + "%";
        int idx = indexOfIgnoreCase(s, token);
        if (idx < 0) {
            return s;
        }
        String val = System.getenv(name);
        if (val == null || val.trim().isEmpty()) {
            if ("SystemDrive".equalsIgnoreCase(name)) {
                val = "C:";
            } else {
                return s;
            }
        }
        return s.substring(0, idx) + val + s.substring(idx + token.length());
    }

    private static int indexOfIgnoreCase(String s, String token) {
        return indexOfIgnoreCase(s, token, 0);
    }

    private static int indexOfIgnoreCase(String s, String token, int from) {
        if (s == null || token == null || from >= s.length()) {
            return -1;
        }
        return s.toLowerCase().indexOf(token.toLowerCase(), from);
    }

    /**
     * 名称是否通过 --allow-iis 额外名单。空名单表示不额外限制，只走路径判断。
     */
    static boolean nameAllowed(List<String> allowIis, String target, String name) {
        if (allowIis == null || allowIis.isEmpty()) {
            return true;
        }
        String object = "site".equals(target) ? "site" : "apppool";
        String want = name == null ? "" : name.trim();
        for (String raw : allowIis) {
            String item = raw == null ? "" : raw.trim();
            if (item.isEmpty()) {
                continue;
            }
            String t = "";
            String n = item;
            int idx = item.indexOf(':');
            if (idx > 0) {
                t = item.substring(0, idx).trim().toLowerCase();
                n = item.substring(idx + 1).trim();
            }
            if (!n.equalsIgnoreCase(want)) {
                continue;
            }
            if (t.isEmpty() || t.equals(object) || "iis".equals(t)) {
                return true;
            }
        }
        return false;
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

    static List<String> empty() {
        return new ArrayList<String>();
    }
}
