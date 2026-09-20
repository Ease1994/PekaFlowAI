package com.pekaflow.agent;

import java.io.File;
import java.io.FileOutputStream;
import java.io.RandomAccessFile;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;
import java.util.zip.ZipOutputStream;

/**
 * 部署节点的落盘安全行为。这里每一条都对应一种「生产站点被写坏」的具体走法，
 * 不进 jar。
 *
 * 编译：javac -cp out test/NodeSafetyTest.java -d out
 * 运行：java -cp out com.pekaflow.agent.NodeSafetyTest
 */
public final class NodeSafetyTest {
    private static int failed;

    public static void main(String[] args) throws Exception {
        testSafeChildKeepsLegitDoubleDotFileNames();
        testSafeChildBlocksTraversal();
        testExtractReplacesFileAndLeavesNoTemp();
        testFailedExtractLeavesProductionFileIntact();
        testResolveAllowedRejectsOutside();
        testBackupRootRejectsInsideSite();
        testDeriveBackupRootFromInstallDir();
        testFolderNameRejectsDotDot();
        testCleanupOnlyDeletesStampDirs();
        testCleanupSkipsWrongDepth();
        testCleanupSkipsDirsWithoutMarker();
        testCleanupNeverDeletesCurrentStamp();
        testCleanupUnknownPipelineUsesStricterQuota();
        testCleanupCapsPerRun();
        testHasEnoughSpaceRejectsZero();
        testGetProcessPidNeverUsesSelf();
        testIisPathGate();
        testIgnoredNicNames();
        testUsableLanIpv4();
        testResolveServerHost();
        testRoleAllowPathsMismatch();
        testNodeNameCharset();
        testBackupRootRejectsWrappingSite();
        testBackupMustStayInConfiguredRoot();
        testPipelineBackupRelativeJoinsConfiguredRoot();
        testShallowUnixAllowPath();
        testJoinUrlRejectsHostOverride();
        testPluginIdentityAndDownloadPath();
        if (failed > 0) {
            System.err.println("FAILED " + failed);
            System.exit(1);
        }
        System.out.println("NodeSafetyTest OK");
    }

    /**
     * jquery..min.js 这种名字真实存在。以前用 contains("..") 判，它会被拒，
     * 而一个条目被拒整次发布就失败——一次好好的发布被判死在文件名上。
     */
    private static void testSafeChildKeepsLegitDoubleDotFileNames() throws Exception {
        File root = tempDir("site");
        for (String name : new String[] {
            "jquery..min.js", "bin/app..bak.dll", "a..b/c..d.txt", "web.config",
        }) {
            try {
                File f = NodeExecutor.safeChild(root, name);
                check("合法名放行 " + name,
                        f.getCanonicalPath().startsWith(root.getCanonicalPath()));
            } catch (Exception e) {
                check("合法名放行 " + name + "（被拒：" + e.getMessage() + "）", false);
            }
        }
    }

    private static void testSafeChildBlocksTraversal() throws Exception {
        File root = tempDir("site");
        List<String> bad = new ArrayList<String>(Arrays.asList(
                "../evil.dll",
                "..\\evil.dll",
                "bin/../../evil.dll",
                "..",
                "/etc/passwd",
                "C:\\Windows\\System32\\evil.dll",
                ""));
        if (File.separatorChar == '\\') {
            // Windows 备用数据流：路径还在站点目录里，规范化比对拦不住
            bad.add("web.config:evil");
        }
        for (String name : bad) {
            boolean rejected = false;
            try {
                NodeExecutor.safeChild(root, name);
            } catch (IllegalArgumentException e) {
                rejected = true;
            }
            check("越界路径被拒 " + name, rejected);
        }
    }

    private static void testExtractReplacesFileAndLeavesNoTemp() throws Exception {
        File dir = tempDir("extract-ok");
        File zip = new File(dir, "pkg.zip");
        writeZip(zip, "app.dll", bytes("NEW", 4096));

        File dest = new File(dir, "app.dll");
        write(dest, bytes("OLD", 4096));

        ZipFile zf = new ZipFile(zip);
        try {
            FileTransfer.extract(zf, "app.dll", dest);
        } finally {
            zf.close();
        }
        check("内容已替换", startsWith(dest, "NEW"));
        check("没留下临时文件", listTemps(dir).isEmpty());
    }

    /**
     * 这条是这次改动的核心保证。
     *
     * 以前 extract 直接对 dest 开 FileOutputStream，那一下就把线上文件截成 0 字节，
     * 之后包一坏、盘一满，站点里就躺着一个空的 DLL——用旧代码跑这条测试，
     * dest 会变成 0 字节，测试失败。
     */
    private static void testFailedExtractLeavesProductionFileIntact() throws Exception {
        File dir = tempDir("extract-fail");
        File zip = new File(dir, "pkg.zip");
        writeZip(zip, "app.dll", bytes("NEW", 65536));
        corruptEntryData(zip, "app.dll");

        File dest = new File(dir, "app.dll");
        byte[] old = bytes("OLD", 4096);
        write(dest, old);

        boolean threw = false;
        ZipFile zf = new ZipFile(zip);
        try {
            FileTransfer.extract(zf, "app.dll", dest);
        } catch (Exception e) {
            threw = true;
        } finally {
            zf.close();
        }
        check("坏包要报错，不能静悄悄成功", threw);
        check("线上文件长度没变", dest.length() == old.length);
        check("线上文件内容还是旧版本", startsWith(dest, "OLD"));
        check("失败后没在站点目录留临时文件", listTemps(dir).isEmpty());
    }

    private static void testResolveAllowedRejectsOutside() throws Exception {
        File allowed = tempDir("wwwroot");
        List<String> allow = Arrays.asList(allowed.getCanonicalPath());

        check("目录本身放行",
                NodeExecutor.resolveAllowed(allow, allowed.getCanonicalPath()) != null);
        check("子目录放行",
                NodeExecutor.resolveAllowed(allow, new File(allowed, "site1").getPath()) != null);

        expectReject("白名单外的目录",
                allow, new File(allowed.getParentFile(), "elsewhere").getPath());
        expectReject("用 .. 爬出白名单",
                allow, new File(allowed, "..").getPath());
        // 没配白名单时一律拒绝，宁可发不出去也不能让生产机任人写盘
        expectReject("没配白名单", new ArrayList<String>(), allowed.getCanonicalPath());
    }

    private static void expectReject(String label, List<String> allow, String raw) {
        boolean rejected = false;
        try {
            NodeExecutor.resolveAllowed(allow, raw);
        } catch (IllegalArgumentException e) {
            rejected = true;
        } catch (Exception e) {
            rejected = true;
        }
        check("拒绝：" + label, rejected);
    }

    /** 备份含 DLL 和 web.config，落在站点里会被 IIS 当静态文件下载出去。 */
    private static void testBackupRootRejectsInsideSite() throws Exception {
        File site = tempDir("wwwroot");
        List<String> allow = Arrays.asList(site.getCanonicalPath());
        boolean rejected = false;
        try {
            NodeExecutor.resolveBackupRoot(allow, new File(site, "backup").getPath());
        } catch (IllegalArgumentException e) {
            rejected = true;
        }
        check("备份目录不能落在站点里", rejected);

        File outside = new File(site.getParentFile(), "release-backup");
        check("站点之外的备份目录放行",
                NodeExecutor.resolveBackupRoot(allow, outside.getPath()) != null);
    }

    /** 备份跟安装命令所在盘走：第一层 /data、/mnt、/opt，不进允许目录。 */
    private static void testDeriveBackupRootFromInstallDir() {
        check("/data/soft/release → /data/release-backup",
                "/data/release-backup".equals(NodeExecutor.deriveBackupRootFromInstallDir("/data/soft/release")));
        check("/mnt/release/ → /mnt/release-backup",
                "/mnt/release-backup".equals(NodeExecutor.deriveBackupRootFromInstallDir("/mnt/release/")));
        check("/opt/app/release → /opt/release-backup",
                "/opt/release-backup".equals(NodeExecutor.deriveBackupRootFromInstallDir("/opt/app/release")));
        check("/var 第一层不推", NodeExecutor.deriveBackupRootFromInstallDir("/var/release") == null);
        check("根目录不推", NodeExecutor.deriveBackupRootFromInstallDir("/") == null);
        check("/. 不推", NodeExecutor.deriveBackupRootFromInstallDir("/.") == null);
        check("/.. 不推", NodeExecutor.deriveBackupRootFromInstallDir("/..") == null);
        check("/./release 不推", NodeExecutor.deriveBackupRootFromInstallDir("/./release") == null);
        check("/release-backup 算挂在根下", NodeExecutor.isDirectlyUnderUnixRoot("/release-backup"));
        check("/ 算挂在根下", NodeExecutor.isDirectlyUnderUnixRoot("/"));
        check("/data/release-backup 不是根下", !NodeExecutor.isDirectlyUnderUnixRoot("/data/release-backup"));
        check("/var/release/backup 不是根下", !NodeExecutor.isDirectlyUnderUnixRoot("/var/release/backup"));
    }

    /** `.` / `..` 会让备份父目录爬出备份根，清理时可能删到站点或盘符。 */
    private static void testFolderNameRejectsDotDot() {
        check(".. 不能当目录名", "unknown-project".equals(FileTransfer.folderName("..", "unknown-project")));
        check(". 不能当目录名", "unknown-pipeline".equals(FileTransfer.folderName(".", "unknown-pipeline")));
        check("空串走 fallback", "unknown-project".equals(FileTransfer.folderName("  ", "unknown-project")));
        check("正常中文名保留", "订单生产".equals(FileTransfer.folderName("订单生产", "x")));
    }

    /**
     * IIS 启停主闸是物理路径，不是应用池名字名单。
     * 查出来的路径必须落在允许目录内；变量展开不了、配置里抽不出路径，都不能放行。
     */
    private static void testIisPathGate() throws Exception {
        File site = tempDir("iis-site");
        String allowed = site.getCanonicalPath();
        List<String> allow = Arrays.asList(allowed);
        check("白名单内放行", IisControl.pathAllowed(allow, allowed));
        File other = tempDir("iis-other");
        check("白名单外拒绝", !IisControl.pathAllowed(allow, other.getCanonicalPath()));
        check("未知环境变量拒绝", !IisControl.pathAllowed(allow, "%FOO%\\site"));
        check("空路径拒绝", !IisControl.pathAllowed(allow, "  "));

        String expanded = IisControl.expandWinEnv("%SystemDrive%\\wwwroot");
        check("展开 SystemDrive", expanded != null && expanded.indexOf('%') < 0);
        check("未知变量返回 null", IisControl.expandWinEnv("%FOO%\\site") == null);

        List<String> lines = IisControl.parsePathLines("D:\\wwwroot\\a\n\n  E:\\data  \n");
        check("解析两行路径", lines.size() == 2 && "E:\\data".equals(lines.get(1)));

        String xml = "<application path=\"/\">"
                + "<virtualDirectory path=\"/\" physicalPath=\"D:\\wwwroot\\site\" />"
                + "<virtualDirectory path=\"/files\" physicalPath=\"E:\\data\" />"
                + "</application>";
        List<String> fromXml = IisControl.parsePhysicalPathsFromConfig(xml);
        check("配置里抽出两个物理路径", fromXml.size() == 2
                && fromXml.contains("D:\\wwwroot\\site") && fromXml.contains("E:\\data"));
        check("配置里数到一个应用", IisControl.countAppsInConfig(xml) == 1);

        check("空 --allow-iis 不额外限制", IisControl.nameAllowed(IisControl.empty(), "apppool", "AppPool"));
        check("额外名单命中", IisControl.nameAllowed(Arrays.asList("AppPool"), "apppool", "AppPool"));
        check("额外名单未命中", !IisControl.nameAllowed(Arrays.asList("OtherPool"), "apppool", "AppPool"));
    }

    /**
     * 清理只能动 yyyyMMdd-HHmmss 这种备份份目录。
     * 同层的其它文件夹（误放的站点、另一个项目）必须原样留下。
     */
    private static void testCleanupOnlyDeletesStampDirs() throws Exception {
        File root = tempDir("backup-root");
        File pipe = new File(new File(root, "proj"), "pipe");
        if (!pipe.mkdirs()) {
            throw new IllegalStateException("建流水线备份目录失败");
        }
        File site = tempDir("site-untouch");
        File keepMe = new File(pipe, "wwwroot");
        if (!keepMe.mkdirs()) {
            throw new IllegalStateException("建诱饵目录失败");
        }
        File marker = new File(keepMe, "app.dll");
        write(marker, bytes("LIVE", 8));
        File otherProj = new File(new File(root, "other-proj"), "pipe");
        if (!otherProj.mkdirs()) {
            throw new IllegalStateException("建其它项目目录失败");
        }
        File otherStamp = new File(otherProj, "20260101-000000");
        if (!otherStamp.mkdirs()) {
            throw new IllegalStateException("建其它项目备份失败");
        }

        File latest = null;
        for (int i = 1; i <= 25; i++) {
            String stamp = String.format("20260101-%06d", i);
            File d = new File(pipe, stamp);
            if (!d.mkdirs()) {
                throw new IllegalStateException("建备份份失败 " + stamp);
            }
            markBackup(d);
            latest = d;
        }
        LogSink sink = new LogSink() {
            public void log(String line) {
                System.out.println(line);
            }
        };
        FileTransfer.cleanupOldBackups(latest, root, site, 20, sink);

        check("非日期目录未被删", keepMe.isDirectory() && marker.isFile());
        check("其它项目的备份未被删", otherStamp.isDirectory());
        int stamps = 0;
        File[] kids = pipe.listFiles();
        if (kids != null) {
            for (File k : kids) {
                if (FileTransfer.isBackupStampName(k.getName())) {
                    stamps++;
                }
            }
        }
        check("只留最近 20 份日期备份", stamps == 20);
        check("最旧的那份已删", !new File(pipe, "20260101-000001").exists());
        check("第 6 旧的还在", new File(pipe, "20260101-000006").isDirectory());
    }

    /** 备份若少了一层，父目录就是备份根。这时必须整段跳过，不能把各项目文件夹当旧备份删。 */
    private static void testCleanupSkipsWrongDepth() throws Exception {
        File root = tempDir("backup-shallow");
        File projectFolder = new File(root, "real-project");
        if (!projectFolder.mkdirs()) {
            throw new IllegalStateException("建项目目录失败");
        }
        File fakeStamp = new File(root, "20260101-120000");
        if (!fakeStamp.mkdirs()) {
            throw new IllegalStateException("建浅层日期目录失败");
        }
        LogSink sink = new LogSink() {
            public void log(String line) {
                System.out.println(line);
            }
        };
        FileTransfer.cleanupOldBackups(fakeStamp, root, null, 1, sink);
        check("层级不对时不删项目目录", projectFolder.isDirectory());
        check("层级不对时连这份日期目录也不当旧备份清", fakeStamp.isDirectory());
    }

    /** 只有日期名、没有本插件标记文件的目录，可能是人手建的，不能当旧备份删。 */
    private static void testCleanupSkipsDirsWithoutMarker() throws Exception {
        File root = tempDir("backup-nomarker");
        File pipe = new File(new File(root, "proj"), "pipe");
        if (!pipe.mkdirs()) {
            throw new IllegalStateException("建流水线备份目录失败");
        }
        File decoy = new File(pipe, "20260101-000001");
        if (!decoy.mkdirs()) {
            throw new IllegalStateException("建无标记日期目录失败");
        }
        File keepFile = new File(decoy, "do-not-delete.txt");
        write(keepFile, bytes("KEEP", 8));
        File latest = new File(pipe, "20260101-000002");
        if (!latest.mkdirs()) {
            throw new IllegalStateException("建当前备份失败");
        }
        markBackup(latest);
        LogSink sink = silentSink();
        FileTransfer.cleanupOldBackups(latest, root, null, 1, sink);
        check("无标记的日期目录仍在", decoy.isDirectory() && keepFile.isFile());
        check("当前备份仍在", latest.isDirectory());
    }

    /** 机器时钟回拨时，本次备份会排到最旧。按名字排也不能删刚写好的这一份。 */
    private static void testCleanupNeverDeletesCurrentStamp() throws Exception {
        File root = tempDir("backup-clock");
        File pipe = new File(new File(root, "proj"), "pipe");
        if (!pipe.mkdirs()) {
            throw new IllegalStateException("建流水线备份目录失败");
        }
        File current = new File(pipe, "20200101-000000");
        if (!current.mkdirs()) {
            throw new IllegalStateException("建回拨的当前备份失败");
        }
        markBackup(current);
        File newer = new File(pipe, "20260101-120000");
        if (!newer.mkdirs()) {
            throw new IllegalStateException("建较新备份失败");
        }
        markBackup(newer);
        FileTransfer.cleanupOldBackups(current, root, null, 1, silentSink());
        check("时钟回拨时当前备份仍在", current.isDirectory());
        check("较新的那份可以被清", !newer.exists());
    }

    /**
     * unknown-* 目录按更严的份数清，避免缺项目名时备份永不删除把盘写满。
     */
    private static void testCleanupUnknownPipelineUsesStricterQuota() throws Exception {
        File root = tempDir("backup-unknown");
        File pipe = new File(new File(root, "unknown-project"), "unknown-pipeline");
        if (!pipe.mkdirs()) {
            throw new IllegalStateException("建 fallback 备份目录失败");
        }
        File latest = null;
        for (int i = 1; i <= 10; i++) {
            File d = new File(pipe, String.format("20260101-%06d", i));
            if (!d.mkdirs()) {
                throw new IllegalStateException("建 fallback 备份份失败");
            }
            markBackup(d);
            latest = d;
        }
        FileTransfer.cleanupOldBackups(latest, root, null, 20, silentSink());
        int stamps = countOurStamps(pipe);
        check("unnamed 目录最多留 " + FileTransfer.FALLBACK_MAX_KEEP + " 份",
                stamps == FileTransfer.FALLBACK_MAX_KEEP);
        check("当前这份还在", latest != null && latest.isDirectory());
        check("最旧的已被清", !new File(pipe, "20260101-000001").exists());
    }

    /** 超限很多时一次只收一批，避免一次误判把历史清空。 */
    private static void testCleanupCapsPerRun() throws Exception {
        File root = tempDir("backup-cap");
        File pipe = new File(new File(root, "proj"), "pipe");
        if (!pipe.mkdirs()) {
            throw new IllegalStateException("建流水线备份目录失败");
        }
        File latest = null;
        int total = 20 + FileTransfer.MAX_CLEANUP_PER_RUN + 10;
        for (int i = 1; i <= total; i++) {
            File d = new File(pipe, String.format("20260101-%06d", i));
            if (!d.mkdirs()) {
                throw new IllegalStateException("建备份份失败");
            }
            markBackup(d);
            latest = d;
        }
        FileTransfer.cleanupOldBackups(latest, root, null, 20, silentSink());
        int stamps = countOurStamps(pipe);
        check("单次最多清 " + FileTransfer.MAX_CLEANUP_PER_RUN + " 份",
                stamps == total - FileTransfer.MAX_CLEANUP_PER_RUN);
        check("最旧的一批已删", !new File(pipe, "20260101-000001").exists());
        check("超出单次清理窗口的还在",
                new File(pipe, String.format("20260101-%06d", FileTransfer.MAX_CLEANUP_PER_RUN + 1))
                        .isDirectory());
    }

    /** 剩余空间为 0 或读失败必须拒绝，不能当成未知放行。 */
    private static void testHasEnoughSpaceRejectsZero() {
        check("剩余 0 拒绝", !FileTransfer.hasEnoughSpace(0, 1));
        check("读失败拒绝", !FileTransfer.hasEnoughSpace(-1, 1024));
        check("不需要写入时放行", FileTransfer.hasEnoughSpace(0, 0));
        check("空间充足放行", FileTransfer.hasEnoughSpace(2L * 1024 * 1024 * 1024, 1024));
        check("低于预留拒绝", !FileTransfer.hasEnoughSpace(100, 1024));
    }

    /** 取不到子进程 pid 时必须返回 -1，不能回退成 Agent 自己的 PID。 */
    private static void testGetProcessPidNeverUsesSelf() {
        long self = readSelfPid();
        check("空进程返回 -1", TaskExecutor.getProcessPid(null) == -1);
        check("当前进程 pid 能读到", self > 0);
    }

    private static long readSelfPid() {
        try {
            String name = java.lang.management.ManagementFactory.getRuntimeMXBean().getName();
            int at = name.indexOf('@');
            if (at > 0) {
                return Long.parseLong(name.substring(0, at));
            }
        } catch (Exception ignored) {
        }
        return -1;
    }

    private static void markBackup(File dir) throws Exception {
        File marker = new File(dir, FileTransfer.BACKUP_MARKER);
        write(marker, bytes("marker", 8));
    }

    private static int countOurStamps(File pipe) {
        int n = 0;
        File[] kids = pipe.listFiles();
        if (kids != null) {
            for (File k : kids) {
                if (FileTransfer.isOurBackupStamp(k)) {
                    n++;
                }
            }
        }
        return n;
    }

    private static LogSink silentSink() {
        return new LogSink() {
            public void log(String line) {
            }
        };
    }

    // ================= 工具 =================

    /**
     * 把条目的压缩数据头几个字节涂掉，让读到一半炸。
     *
     * 只动本地头后面的载荷，文件末尾的中央目录不碰，所以 ZipFile 能正常打开，
     * 报错发生在读数据的时候——这才是「包坏了」在生产上的真实样子。
     * 0xFF 开头的 deflate 流是保留块类型，必定抛异常，不靠概率。
     */
    private static void corruptEntryData(File zip, String entryName) throws Exception {
        long dataStart = 30L + entryName.getBytes("UTF-8").length;
        RandomAccessFile raf = new RandomAccessFile(zip, "rw");
        try {
            raf.seek(dataStart);
            byte[] junk = new byte[32];
            Arrays.fill(junk, (byte) 0xFF);
            raf.write(junk);
        } finally {
            raf.close();
        }
    }

    private static void writeZip(File zip, String entryName, byte[] content) throws Exception {
        ZipOutputStream out = new ZipOutputStream(new FileOutputStream(zip));
        try {
            out.putNextEntry(new ZipEntry(entryName));
            out.write(content);
            out.closeEntry();
        } finally {
            out.close();
        }
    }

    private static byte[] bytes(String prefix, int total) throws Exception {
        StringBuilder sb = new StringBuilder(prefix);
        while (sb.length() < total) {
            sb.append("-padding");
        }
        return sb.substring(0, total).getBytes("UTF-8");
    }

    private static void write(File f, byte[] content) throws Exception {
        FileOutputStream out = new FileOutputStream(f);
        try {
            out.write(content);
        } finally {
            out.close();
        }
    }

    private static boolean startsWith(File f, String prefix) throws Exception {
        byte[] want = prefix.getBytes("UTF-8");
        if (f.length() < want.length) {
            return false;
        }
        java.io.FileInputStream in = new java.io.FileInputStream(f);
        try {
            byte[] got = new byte[want.length];
            int n = in.read(got);
            return n == want.length && Arrays.equals(want, got);
        } finally {
            in.close();
        }
    }

    private static List<String> listTemps(File dir) {
        List<String> out = new ArrayList<String>();
        String[] names = dir.list();
        if (names != null) {
            for (String n : names) {
                if (n.endsWith(".rp-tmp")) {
                    out.add(n);
                }
            }
        }
        return out;
    }

    /** docker0 / br-xxxx 不能当节点 IP；br0 是业务网桥要保留。 */
    private static void testIgnoredNicNames() {
        check("docker0 忽略", Config.isIgnoredNicName("docker0"));
        check("br-hash 忽略", Config.isIgnoredNicName("br-1a2b3c4d5e6f"));
        check("veth 忽略", Config.isIgnoredNicName("veth1234"));
        check("br0 保留", !Config.isIgnoredNicName("br0"));
        check("ens192 保留", !Config.isIgnoredNicName("ens192"));
        check("eth0 保留", !Config.isIgnoredNicName("eth0"));
    }

    /** 回环和链路本地不能写到节点列表。 */
    private static void testUsableLanIpv4() throws Exception {
        check("业务网 IP 可用", Config.isUsableLanIpv4(java.net.InetAddress.getByName("172.18.10.242")));
        check("回环不可用", !Config.isUsableLanIpv4(java.net.InetAddress.getByName("127.0.0.1")));
        check("链路本地不可用", !Config.isUsableLanIpv4(java.net.InetAddress.getByName("169.254.1.1")));
    }

    private static void testResolveServerHost() throws Exception {
        java.net.InetAddress got = Config.resolveServerHost("http://172.18.10.233:8080");
        check("从平台 URL 解析主机", got != null && "172.18.10.233".equals(got.getHostAddress()));
        check("空 URL 解析不到", Config.resolveServerHost("") == null);
    }

    /** 生产机带了 allow-paths 却忘了 --role node，必须起不来。 */
    private static void testRoleAllowPathsMismatch() {
        boolean rejected = false;
        try {
            Config.parse(new String[] {"--allow-paths", "D:\\wwwroot\\site", "--name", "oops"});
        } catch (IllegalArgumentException e) {
            rejected = e.getMessage() != null && e.getMessage().contains("--role node");
        }
        check("有 allow-paths 却是 builder 时启动失败", rejected);
    }

    private static void testNodeNameCharset() {
        check("合法节点名", Config.isSafeNodeName("web-prod-01"));
        check("中文节点名放行", Config.isSafeNodeName("AI大奋"));
        check("空格节点名拒绝", !Config.isSafeNodeName("web RUN_USER=root"));
        check("分号拒绝", !Config.isSafeNodeName("web;id"));
        check("空名称拒绝", !Config.isSafeNodeName(""));
        boolean rejected = false;
        try {
            Config.parse(new String[] {"--role", "node", "--name", "web RUN_USER=root"});
        } catch (IllegalArgumentException e) {
            rejected = true;
        }
        check("节点名带空格启动失败", rejected);
    }

    /** 备份根包住站点时，清理备份可能碰到站点文件，安装时 chown 也会改属主。 */
    private static void testBackupRootRejectsWrappingSite() throws Exception {
        File site = tempDir("site-wrap");
        File parent = site.getParentFile();
        List<String> allow = Arrays.asList(site.getCanonicalPath());
        boolean rejected = false;
        try {
            NodeExecutor.resolveBackupRoot(allow, parent.getPath());
        } catch (IllegalArgumentException e) {
            rejected = e.getMessage() != null && e.getMessage().contains("包住");
        }
        check("备份根不能包住站点", rejected);
    }

    private static void testBackupMustStayInConfiguredRoot() throws Exception {
        File root = tempDir("bak-root");
        File inside = new File(root, "proj");
        if (!inside.mkdirs() && !inside.isDirectory()) {
            throw new IllegalStateException("建备份子目录失败");
        }
        File outside = tempDir("elsewhere");
        NodeExecutor.assertInsideConfiguredBackupRoot(inside, root.getCanonicalPath());
        boolean rejected = false;
        try {
            NodeExecutor.assertInsideConfiguredBackupRoot(outside, root.getCanonicalPath());
        } catch (IllegalArgumentException e) {
            rejected = true;
        }
        check("流水线备份必须落在安装时的备份根下", rejected);
        NodeExecutor.assertInsideConfiguredBackupRoot(outside, "");
        check("老 Agent 没配备份根时不拦这一层", true);
    }

    /**
     * 流水线自定义备份：相对路径接到节点备份根下；根外的绝对路径和跳出根的 .. 都拒绝。
     */
    private static void testPipelineBackupRelativeJoinsConfiguredRoot() throws Exception {
        File root = tempDir("pipe-bak");
        File site = tempDir("pipe-site");
        List<String> allow = Arrays.asList(site.getCanonicalPath());
        File joined = NodeExecutor.resolvePipelineBackupRoot(allow, "archive", root.getCanonicalPath());
        File expect = new File(root, "archive").getCanonicalFile();
        check("相对路径接到节点备份根下", joined.getCanonicalPath().equals(expect.getPath()));

        File insideAbs = new File(root, "special");
        if (!insideAbs.mkdirs() && !insideAbs.isDirectory()) {
            throw new IllegalStateException("建绝对子目录失败");
        }
        File absOk = NodeExecutor.resolvePipelineBackupRoot(
                allow, insideAbs.getCanonicalPath(), root.getCanonicalPath());
        check("备份根下的绝对路径放行", absOk.getCanonicalPath().equals(insideAbs.getCanonicalPath()));

        File outside = tempDir("pipe-out");
        boolean rejected = false;
        try {
            NodeExecutor.resolvePipelineBackupRoot(allow, outside.getCanonicalPath(), root.getCanonicalPath());
        } catch (IllegalArgumentException e) {
            rejected = true;
        }
        check("备份根外的绝对路径拒绝", rejected);

        boolean escaped = false;
        try {
            NodeExecutor.resolvePipelineBackupRoot(
                    allow, ".." + File.separator + "escape", root.getCanonicalPath());
        } catch (IllegalArgumentException e) {
            escaped = true;
        }
        check("相对路径不能跳出备份根", escaped);

        boolean relativeNeedsRoot = false;
        try {
            NodeExecutor.resolvePipelineBackupRoot(allow, "archive", "");
        } catch (IllegalArgumentException e) {
            relativeNeedsRoot = e.getMessage() != null && e.getMessage().contains("备份根");
        }
        check("没配备份根时相对路径拒绝", relativeNeedsRoot);
    }

    private static void testShallowUnixAllowPath() {
        check("/data 是一层", NodeExecutor.isDirectlyUnderUnixRoot("/data"));
        check("/mnt 是一层", NodeExecutor.isDirectlyUnderUnixRoot("/mnt"));
        check("/data/nginx 不是一层", !NodeExecutor.isDirectlyUnderUnixRoot("/data/nginx"));
        if (File.separatorChar == '/') {
            List<String> rejected = new ArrayList<String>();
            List<String> cleaned = NodeExecutor.sanitizeAllowPaths(Arrays.asList("/data", "/tmp"), rejected);
            check("Linux 丢掉一层路径和临时目录", cleaned.isEmpty());
            check("被拒原因有记录", !rejected.isEmpty());
        }
    }

    private static void testJoinUrlRejectsHostOverride() {
        boolean at = false;
        try {
            ApiClient.joinUrl("http://127.0.0.1:8080", "@evil.example/malware.zip");
        } catch (IllegalArgumentException e) {
            at = true;
        }
        boolean slash = false;
        try {
            ApiClient.joinUrl("http://127.0.0.1:8080", "//evil.example/malware.zip");
        } catch (IllegalArgumentException e) {
            slash = true;
        }
        check("@host 不能拼进平台地址", at);
        check("//host 不能拼进平台地址", slash);
        check("合法商店路径",
                "http://127.0.0.1:8080/api/v1/store/plugins/git-checkout/package"
                        .equals(ApiClient.joinUrl("http://127.0.0.1:8080",
                                "/api/v1/store/plugins/git-checkout/package")));
    }

    private static void testPluginIdentityAndDownloadPath() {
        check("合法插件名", TaskExecutor.isSafePluginIdentity("git-checkout"));
        check("合法版本", TaskExecutor.isSafePluginIdentity("1.0.8"));
        check("路径型插件名拒绝", !TaskExecutor.isSafePluginIdentity("../.ssh"));
        check("商店路径放行", TaskExecutor.isPluginDownloadPath("/api/v1/store/plugins/git-checkout/package"));
        check("草稿路径放行", TaskExecutor.isPluginDownloadPath("/api/v1/store/plugin-drafts/12/package"));
        check("@ 下载地址拒绝", !TaskExecutor.isPluginDownloadPath("@evil.example/x.zip"));
        check("外网 URL 拒绝", !TaskExecutor.isPluginDownloadPath("https://evil.example/x.zip"));
    }

    private static File tempDir(String label) throws Exception {
        File base = File.createTempFile("rp-" + label + "-", "");
        base.delete();
        File dir = new File(base, "root");
        if (!dir.mkdirs()) {
            throw new IllegalStateException("建临时目录失败：" + dir);
        }
        deleteOnExit(base);
        return dir;
    }

    private static void deleteOnExit(File f) {
        f.deleteOnExit();
        File[] kids = f.listFiles();
        if (kids != null) {
            for (File k : kids) {
                deleteOnExit(k);
            }
        }
    }

    private static void check(String label, boolean ok) {
        if (!ok) {
            failed++;
            System.err.println("FAIL " + label);
        } else {
            System.out.println("ok   " + label);
        }
    }
}
