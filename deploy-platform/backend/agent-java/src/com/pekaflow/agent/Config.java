package com.pekaflow.agent;

import java.io.File;
import java.net.DatagramSocket;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.SocketException;
import java.net.URL;
import java.util.ArrayList;
import java.util.Enumeration;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

/** Agent 运行配置。 */
public class Config {
    public String serverUrl = "http://localhost:8080";
    public String name;
    public String host;
    public String os;
    public List<String> tags = new ArrayList<>();
    public int pollInterval = 2;   // 拉任务间隔（秒）——空闲时上限；有空位时会更快
    public int concurrency = 8;    // 同时执行的最大任务数（1~32）
    public int agentId = 0;        // 注册后由服务端返回
    public String token = "";      // 注册后由服务端返回，后续直连接口凭此鉴权
    public String enrollToken = "";  // 接入凭证：新构建机首次注册必须带，之后靠落盘的 token 续期
    public String workspaceRoot = "workspace";  // 工作空间根目录（默认 ./workspace）
    /**
     * builder=构建机（编译打包，能跑插件和脚本）；node=部署节点。
     *
     * 节点跑在生产服务器上，只执行内置的发布动作：不下载插件、不执行任意脚本，
     * 所有落盘操作都必须在 allowPaths 之内。
     */
    public String role = "builder";
    public List<String> allowPaths = new ArrayList<>();
    /**
     * 允许控制的服务，形如 systemd:nginx、docker:web；省略类型按 systemd 解释。
     *
     * 和 allowPaths 同样是画在本机的线。机器上还有一道 sudoers 白名单兜底，
     * 那道线连 Agent 启动参数被改了也绕不开。
     */
    public List<String> allowServices = new ArrayList<>();
    /**
     * 可选的 IIS 名字额外收紧，形如 DefaultAppPool、site:www。
     *
     * 启停是否放行，主闸是物理路径是否落在 allowPaths 内。
     * 这份名单只在启动参数写了 --allow-iis 时再生效；空 = 不额外限制。
     */
    public List<String> allowIis = new ArrayList<>();
    /**
     * 节点备份根。安装脚本按执行安装命令的目录第一层建好（/data/soft/release → /data/release-backup）。
     * 必须至少两层（不能是 / 或 /release-backup），流水线 backupDir 留空时用这里。空则 Linux 退到 /var/release/backup。
     */
    public String backupRoot = "";
    /**
     * 环境码。只在平台第一次见到这台机器时用得上，之后以页面上的设置为准。
     *
     * 默认 prod。合法码原样上报（prod/test/uat/staging/dev 或自定义）；
     * 非法值启动失败，绝不默默改成 prod——否则 --env uat 会被吃掉，隔离形同虚设。
     */
    public String env = "prod";
    private static final Pattern ENV_SLUG = Pattern.compile("^[a-z][a-z0-9_-]{0,15}$");
    /**
     * Agent 家目录：放登记凭据、工作区、下载的包和备份。
     *
     * 必须独立于 user.home——守护进程（Windows 计划任务 / systemd）通常以 SYSTEM
     * 或 root 身份拉起，user.home 会变成另一个目录，凭据就找不着了。
     */
    public String home = "";
    /** 当前 jar 的指纹（sha256 前 12 位），用于和平台比对是否需要升级。 */
    public String version = "";

    /** 解析命令行参数（缺值/空值不抛异常）。 */
    public static Config parse(String[] args) {
        Config c = new Config();
        c.os = detectOs();
        for (int i = 0; i < args.length; i++) {
            String a = args[i];
            String v = (i + 1 < args.length) ? args[i + 1] : null;
            switch (a) {
                case "--server":
                    if (v != null && !v.trim().isEmpty() && !v.startsWith("--")) {
                        c.serverUrl = v.trim();
                        i++;
                    }
                    break;
                case "--name":
                    if (v != null && !v.trim().isEmpty() && !v.startsWith("--")) {
                        c.name = v.trim();
                        i++;
                    }
                    break;
                case "--tags":
                    if (v != null && !v.startsWith("--")) {
                        for (String t : v.split(",")) {
                            if (!t.trim().isEmpty()) c.tags.add(t.trim());
                        }
                        i++;
                    }
                    break;
                case "--poll":
                    if (v != null && !v.startsWith("--")) {
                        try {
                            int n = Integer.parseInt(v.trim());
                            if (n < 1) n = 1;
                            if (n > 60) n = 60;
                            c.pollInterval = n;
                        } catch (NumberFormatException ignored) {
                            // 非法值保持默认
                        }
                        i++;
                    }
                    break;
                case "--concurrency":
                    if (v != null && !v.startsWith("--")) {
                        try {
                            int n = Integer.parseInt(v.trim());
                            if (n < 1) n = 1;
                            if (n > 32) n = 32;
                            c.concurrency = n;
                        } catch (NumberFormatException ignored) {
                        }
                        i++;
                    }
                    break;
                case "--workspace":
                    if (v != null && !v.trim().isEmpty() && !v.startsWith("--")) {
                        c.workspaceRoot = v.trim();
                        i++;
                    }
                    break;
                case "--enroll-token":
                    if (v != null && !v.trim().isEmpty() && !v.startsWith("--")) {
                        c.enrollToken = v.trim();
                        i++;
                    }
                    break;
                case "--role":
                    if (v != null && !v.trim().isEmpty() && !v.startsWith("--")) {
                        c.role = "node".equalsIgnoreCase(v.trim()) ? "node" : "builder";
                        i++;
                    }
                    break;
                case "--home":
                    if (v != null && !v.trim().isEmpty() && !v.startsWith("--")) {
                        c.home = v.trim();
                        i++;
                    }
                    break;
                case "--allow-paths":
                    if (v != null && !v.startsWith("--")) {
                        for (String p : v.split(";")) {
                            if (!p.trim().isEmpty()) c.allowPaths.add(p.trim());
                        }
                        i++;
                    }
                    break;
                case "--allow-services":
                    if (v != null && !v.startsWith("--")) {
                        // 和 --allow-paths 一样用分号分隔，省得两个参数两种写法
                        for (String s : v.split(";")) {
                            if (!s.trim().isEmpty()) c.allowServices.add(s.trim());
                        }
                        i++;
                    }
                    break;
                case "--allow-iis":
                    if (v != null && !v.startsWith("--")) {
                        for (String s : v.split(";")) {
                            if (!s.trim().isEmpty()) c.allowIis.add(s.trim());
                        }
                        i++;
                    }
                    break;
                case "--backup-root":
                    if (v != null && !v.trim().isEmpty() && !v.startsWith("--")) {
                        c.backupRoot = v.trim();
                        i++;
                    }
                    break;
                case "--env":
                    if (v != null && !v.startsWith("--")) {
                        String e = v.trim().toLowerCase();
                        if (e.isEmpty() || !ENV_SLUG.matcher(e).matches()) {
                            throw new IllegalArgumentException(
                                "--env 不合法「" + v + "」，只允许小写字母开头、最多 16 位的字母数字-_，"
                                + "内置：prod、test、uat、staging、dev");
                        }
                        c.env = e;
                        i++;
                    }
                    break;
                default:
                    // 忽略未知参数
            }
        }
        if (c.enrollToken.isEmpty()) {
            String env = System.getenv("RELEASE_ENROLL_TOKEN");
            if (env != null) {
                c.enrollToken = env.trim();
            }
        }
        if (c.home.isEmpty()) {
            String env = System.getenv("RELEASE_AGENT_HOME");
            c.home = (env != null && !env.trim().isEmpty())
                    ? env.trim()
                    : new File(System.getProperty("user.home", "."), ".release-agent").getAbsolutePath();
        }
        c.version = fingerprintSelf();
        if (c.name == null || c.name.trim().isEmpty()) {
            c.name = "agent-" + (System.currentTimeMillis() % 100000);
        }
        if (c.tags.isEmpty()) {
            c.tags.add(c.os);
        }
        if ("node".equals(c.role)) {
            // 节点不参与标签调度（任务是按节点 ID 指名派发的），并发也没意义：
            // 同一台机器上同时改站点文件只会互相打架
            c.concurrency = 1;
        }
        if (c.serverUrl == null || c.serverUrl.trim().isEmpty()) {
            c.serverUrl = "http://localhost:8080";
        }
        // 去掉 server 末尾斜杠
        while (c.serverUrl.endsWith("/")) {
            c.serverUrl = c.serverUrl.substring(0, c.serverUrl.length() - 1);
        }
        // 生产机带了 --allow-paths 却忘了 --role node 时，默认是 builder：
        // 能下插件、能 bash -c，白名单形同虚设。必须当场退出，不能默默当成构建机。
        if (!c.allowPaths.isEmpty() && !"node".equals(c.role)) {
            throw new IllegalArgumentException(
                    "带了 --allow-paths 却不是 --role node。这样会按构建机启动，能下载插件并执行任意脚本。"
                            + "生产机请加上 --role node；构建机请去掉 --allow-paths");
        }
        if ("node".equals(c.role) && !isSafeNodeName(c.name)) {
            throw new IllegalArgumentException(
                    "--name 不能含空格或 ;|&$<>\\\"'`/ 这类会拆开安装命令的字符（收到「" + c.name + "」）");
        }
        // --server 解析完再探测：本机连平台走哪张网卡，页面上就该显示哪个 IP
        detectServerUrl = c.serverUrl;
        c.host = detectLocalIp(c.serverUrl);
        return c;
    }

    /**
     * 节点名称会进 systemd unit、sudo env 和凭据文件名。
     *
     * 要拦的是空格和分号这类会把安装命令拆成别的赋值的字符
     * （NAME=web RUN_USER=root），不是中文。线上已有「AI大奋」这种名字，
     * 新 jar 若只许 ASCII，升级后进程起不来，节点会一直离线。
     */
    static boolean isSafeNodeName(String name) {
        if (name == null || name.isEmpty() || name.length() > 64) {
            return false;
        }
        for (int i = 0; i < name.length(); i++) {
            char c = name.charAt(i);
            if (c <= 32 || c == 127) {
                return false;
            }
            if ("\"'`$;|&<>\\/".indexOf(c) >= 0) {
                return false;
            }
        }
        return true;
    }

    /** Agent 家目录。 */
    public File homeDir() {
        return new File(home);
    }

    /** 登记凭据的落盘位置：按 server + name 区分，一台机器可接多个平台。 */
    public File credentialFile() {
        return new File(new File(homeDir(), "enrolled"), credentialKey() + ".token");
    }

    private String credentialKey() {
        return (serverUrl + "|" + name).replaceAll("[^A-Za-z0-9._-]", "_");
    }

    /**
     * 旧版本把凭据固定放在 ~/.release-agent 下。守护化之后家目录可能被显式指到
     * ProgramData 之类的公共位置，这里兜底读一次旧路径，避免已装好的机器升级后
     * 要重新拿接入凭证。
     */
    private File legacyCredentialFile() {
        File legacyHome = new File(System.getProperty("user.home", "."), ".release-agent");
        return new File(new File(legacyHome, "enrolled"), credentialKey() + ".token");
    }

    /** 读取上次注册拿到的 token，让重启/升级不必再带接入凭证。 */
    public void loadSavedToken() {
        File f = credentialFile();
        if (!f.isFile()) {
            f = legacyCredentialFile();
            if (!f.isFile()) {
                return;
            }
        }
        try {
            byte[] raw = java.nio.file.Files.readAllBytes(f.toPath());
            token = new String(raw, java.nio.charset.StandardCharsets.UTF_8).trim();
        } catch (Exception ignored) {
            // 读不到就当没登记过，走接入凭证重新注册
        }
    }

    public void saveToken() {
        if (token == null || token.isEmpty()) {
            return;
        }
        File f = credentialFile();
        try {
            File dir = f.getParentFile();
            if (dir != null && !dir.exists()) {
                dir.mkdirs();
            }
            java.nio.file.Files.write(f.toPath(), token.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            restrictToOwner(f);
        } catch (Exception e) {
            System.err.println("[agent] 登记凭据保存失败（下次启动需再带 --enroll-token）: " + e.getMessage());
        }
    }

    /** 平台下发的节点白名单落盘位置。重启后先读这份，再等心跳对齐。 */
    File nodePolicyFile() {
        return new File(homeDir(), "node-policy.json");
    }

    /**
     * 启动时叠加上一次心跳保存的允许目录。
     *
     * 启动参数可能还是第一次安装的旧名单。管理员在页面上加过目录之后，
     * 以这份文件为准，避免重装才能生效。文件坏了或名单被消毒后为空，则继续用启动参数。
     */
    public void loadNodePolicy() {
        File f = nodePolicyFile();
        if (!f.isFile()) {
            return;
        }
        try {
            byte[] raw = java.nio.file.Files.readAllBytes(f.toPath());
            String text = new String(raw, java.nio.charset.StandardCharsets.UTF_8).trim();
            Map<String, Object> obj = Json.obj(Json.parse(text));
            List<Object> arr = Json.arr(obj.get("allow_paths"));
            List<String> next = new ArrayList<String>();
            for (int i = 0; i < arr.size(); i++) {
                String p = Json.str(arr.get(i)).trim();
                if (!p.isEmpty()) {
                    next.add(p);
                }
            }
            List<String> rejected = new ArrayList<String>();
            List<String> cleaned = NodeExecutor.sanitizeAllowPaths(next, rejected);
            if (cleaned.isEmpty()) {
                System.err.println("[agent] 本地策略文件没有可用的允许目录，继续用启动参数");
                return;
            }
            allowPaths.clear();
            allowPaths.addAll(cleaned);
        } catch (Exception e) {
            System.err.println("[agent] 读取节点策略失败，继续用启动参数: " + e.getMessage());
        }
    }

    /**
     * 把当前允许目录写到策略文件。心跳更新后调用，重启不用等下一次心跳。
     */
    public void saveNodePolicy() {
        File f = nodePolicyFile();
        try {
            File dir = f.getParentFile();
            if (dir != null && !dir.exists()) {
                dir.mkdirs();
            }
            Map<String, Object> obj = new java.util.LinkedHashMap<String, Object>();
            obj.put("allow_paths", new ArrayList<String>(allowPaths));
            java.nio.file.Files.write(
                    f.toPath(),
                    Json.stringify(obj).getBytes(java.nio.charset.StandardCharsets.UTF_8));
            restrictToOwner(f);
        } catch (Exception e) {
            System.err.println("[agent] 节点策略保存失败: " + e.getMessage());
        }
    }

    /**
     * 把凭据文件收紧到只有属主能读写。
     *
     * 这个 token 等价于「这台节点的身份」，默认 umask 下同机其他用户是能读到的。
     * 生产机上不止跑我们一个程序，别让一个不相干的服务账号顺手把它读走。
     */
    private static void restrictToOwner(File f) {
        try {
            java.nio.file.Path p = f.toPath();
            if (f.getPath().indexOf(':') == 1 || System.getProperty("os.name", "")
                    .toLowerCase().contains("win")) {
                // Windows 没有 POSIX 权限位，用 File 的接口：先对所有人关掉，再只给属主开
                f.setReadable(false, false);
                f.setWritable(false, false);
                f.setReadable(true, true);
                f.setWritable(true, true);
                return;
            }
            java.nio.file.Files.setPosixFilePermissions(
                    p, java.nio.file.attribute.PosixFilePermissions.fromString("rw-------"));
        } catch (Exception e) {
            // 收权限失败不该让 Agent 起不来，但要说清楚，否则没人知道凭据是敞着的
            System.err.println("[agent] 提示：凭据文件权限收紧失败（" + e.getMessage()
                    + "），请手动确认 " + f.getPath() + " 只有运行账号可读");
        }
    }

    /** 当前运行的 jar 文件；从 class 目录直跑（开发态）时返回 null。 */
    public static File selfJar() {
        try {
            java.net.URL loc = Config.class.getProtectionDomain().getCodeSource().getLocation();
            File f = new File(loc.toURI());
            return f.isFile() && f.getName().endsWith(".jar") ? f : null;
        } catch (Exception ignored) {
            return null;
        }
    }

    /**
     * 用 jar 的 sha256 前 12 位当版本号。
     *
     * 不自己维护版本号是有意的：平台下发的就是这个 jar 文件本身，指纹天然对齐，
     * 不会出现"改了代码忘了改版本号"导致升级不生效。
     */
    static String fingerprintSelf() {
        File jar = selfJar();
        return jar == null ? "dev" : fingerprint(jar);
    }

    /** 文件的 sha256 前 12 位；读不到返回 "unknown"。 */
    public static String fingerprint(File f) {
        try {
            java.security.MessageDigest md = java.security.MessageDigest.getInstance("SHA-256");
            byte[] buf = new byte[8192];
            try (java.io.InputStream in = new java.io.FileInputStream(f)) {
                int n;
                while ((n = in.read(buf)) > 0) {
                    md.update(buf, 0, n);
                }
            }
            StringBuilder sb = new StringBuilder();
            for (byte b : md.digest()) {
                sb.append(String.format("%02x", b));
            }
            return sb.substring(0, 12);
        } catch (Exception ignored) {
            return "unknown";
        }
    }

    static String detectOs() {
        String osName = System.getProperty("os.name", "").toLowerCase();
        if (osName.contains("win")) return "windows";
        if (osName.contains("mac")) return "macos";
        return "linux";
    }

    /**
     * 解析完成后的平台地址。无参 detectLocalIp（例如任务日志）走同一条路由，
     * 避免列表页和日志各报一个 IP。
     */
    static String detectServerUrl = "";

    /**
     * 本机用来访问平台的 IPv4。
     *
     * Linux 上网卡枚举常把 docker0 / br-xxxx 排在 eth/ens 前面，第一张卡的地址
     * 往往是 30.10.0.1 这类网桥网关，不是运维认的业务网 IP。先问内核「去平台
     * 走哪条路由」，没有结果再扫网卡，并跳过容器/网桥接口。
     */
    static String detectLocalIp() {
        return detectLocalIp(detectServerUrl);
    }

    /**
     * @param serverUrl 平台地址，例如 http://172.18.10.233:8080；空则只扫网卡
     */
    static String detectLocalIp(String serverUrl) {
        String via = ipTowardServer(serverUrl);
        if (via != null) {
            return via;
        }
        String nic = firstUsableNicIpv4();
        return nic != null ? nic : "unknown";
    }

    /**
     * 对平台主机做 UDP connect（不发包），内核按路由表选出源地址。
     * 平台是回环地址时返回 null，交给网卡扫描，免得页面上全是 127.0.0.1。
     */
    static String ipTowardServer(String serverUrl) {
        InetAddress target = resolveServerHost(serverUrl);
        if (target == null || target.isLoopbackAddress() || target.isAnyLocalAddress()) {
            return null;
        }
        DatagramSocket socket = null;
        try {
            socket = new DatagramSocket();
            socket.connect(new InetSocketAddress(target, 7));
            InetAddress local = socket.getLocalAddress();
            if (isUsableLanIpv4(local)) {
                return local.getHostAddress();
            }
        } catch (Exception ignored) {
            // 没网、被禁 UDP 都不该让 Agent 起不来
        } finally {
            if (socket != null) {
                socket.close();
            }
        }
        return null;
    }

    /** 从 --server URL 取出主机并解析；解析失败返回 null。 */
    static InetAddress resolveServerHost(String serverUrl) {
        if (serverUrl == null || serverUrl.trim().isEmpty()) {
            return null;
        }
        try {
            URL u = new URL(serverUrl.trim());
            String host = u.getHost();
            if (host == null || host.isEmpty()) {
                return null;
            }
            return InetAddress.getByName(host);
        } catch (Exception ignored) {
            return null;
        }
    }

    /**
     * 扫本机网卡拿第一个能当「这台机器」的 IPv4。
     * docker0、veth、br-xxxx 这类接口跳过，避免把容器网关报成节点 IP。
     */
    static String firstUsableNicIpv4() {
        try {
            Enumeration<NetworkInterface> nis = NetworkInterface.getNetworkInterfaces();
            while (nis != null && nis.hasMoreElements()) {
                NetworkInterface ni = nis.nextElement();
                try {
                    if (ni.isLoopback() || ni.isVirtual() || !ni.isUp()) {
                        continue;
                    }
                } catch (SocketException ignored) {
                    continue;
                }
                if (isIgnoredNicName(ni.getName()) || isIgnoredNicName(ni.getDisplayName())) {
                    continue;
                }
                Enumeration<InetAddress> addrs = ni.getInetAddresses();
                while (addrs.hasMoreElements()) {
                    InetAddress addr = addrs.nextElement();
                    if (isUsableLanIpv4(addr)) {
                        return addr.getHostAddress();
                    }
                }
            }
        } catch (Exception ignored) {
            // ignore
        }
        return null;
    }

    /**
     * 容器网桥、隧道、虚拟以太网：不该出现在节点列表的「IP / 主机」上。
     * br0 这种业务网桥会保留；docker 创建的是 br- 后跟十六进制。
     */
    static boolean isIgnoredNicName(String name) {
        if (name == null || name.isEmpty()) {
            return false;
        }
        String n = name.toLowerCase();
        if (n.equals("lo") || n.equals("lo0") || n.equals("docker0") || n.equals("docker_gwbridge")) {
            return true;
        }
        if (n.startsWith("br-") || n.startsWith("veth") || n.startsWith("virbr")
                || n.startsWith("tun") || n.startsWith("tap") || n.startsWith("cni")
                || n.startsWith("flannel") || n.startsWith("cali") || n.startsWith("weave")
                || n.startsWith("vxlan") || n.startsWith("dummy") || n.startsWith("cilium")
                || n.startsWith("kube-")) {
            return true;
        }
        return n.contains("docker") || n.contains("wsl");
    }

    /** 能写到节点列表上的 IPv4：排除回环、0.0.0.0、169.254 链路本地、组播。 */
    static boolean isUsableLanIpv4(InetAddress addr) {
        return addr instanceof Inet4Address
                && !addr.isLoopbackAddress()
                && !addr.isAnyLocalAddress()
                && !addr.isLinkLocalAddress()
                && !addr.isMulticastAddress();
    }
}
