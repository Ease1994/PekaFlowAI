package com.pekaflow.agent;

import java.io.File;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * HTTP 客户端封装（JDK 8 HttpURLConnection，无第三方依赖）。
 * 后端统一响应格式：{"code":0,"message":"ok","data":{...}}
 */
public class ApiClient {
    static final long MAX_DOWNLOAD_BYTES = 2L * 1024 * 1024 * 1024;
    static final int MAX_JSON_BYTES = 8 * 1024 * 1024;

    private final Config config;

    public ApiClient(Config config) {
        this.config = config;
    }

    public String serverUrl() {
        return config.serverUrl == null ? "" : config.serverUrl;
    }

    public String token() {
        return config.token == null ? "" : config.token;
    }

    public int agentId() {
        return config.agentId;
    }

    /**
     * 平台地址和接口路径的拼接。
     *
     * 任务体里的 download_path 若写成 @evil.com，Java URL 会把 host 解析到外面，
     * 插件包从攻击者那里下，Agent token 也会被带过去。这里只认本站相对路径。
     */
    static String joinUrl(String server, String path) {
        if (server == null || server.trim().isEmpty()) {
            throw new IllegalArgumentException("平台地址为空");
        }
        if (path == null || path.isEmpty() || path.charAt(0) != '/' || path.startsWith("//")) {
            throw new IllegalArgumentException("接口路径必须是以 / 开头的本站路径");
        }
        if (path.indexOf('@') >= 0 || path.indexOf(':') >= 0 || path.indexOf('\\') >= 0) {
            throw new IllegalArgumentException("接口路径含非法字符");
        }
        String[] segs = path.split("/");
        for (int i = 0; i < segs.length; i++) {
            if ("..".equals(segs[i])) {
                throw new IllegalArgumentException("接口路径含 ..");
            }
        }
        String base = server.trim();
        while (base.endsWith("/")) {
            base = base.substring(0, base.length() - 1);
        }
        return base + path;
    }

    /** GET 请求，返回 data 字段（可能为 null）。 */
    public Object get(String path) {
        return request("GET", path, null);
    }

    /** POST JSON 请求，返回 data 字段。 */
    public Object post(String path, Object body) {
        return post(path, body, 15);  // 默认 15s 超时
    }

    /**
     * POST JSON 请求，返回 data 字段。
     * @param readTimeoutSec 读超时（秒），长轮询场景设 60+
     */
    public Object post(String path, Object body, int readTimeoutSec) {
        return request("POST", path, body, readTimeoutSec);
    }

    /** 下载二进制到文件（插件 zip / 增量包）。 */
    public void downloadToFile(String path, File dest) {
        downloadToFile(path, dest, 0L);
    }

    /**
     * 下载二进制到文件。
     *
     * @param deadlineMs 整步截止时间戳（epoch millis）；0 表示不额外限制总时长，
     *                   仍受单次 read 超时和体积上限约束
     */
    public void downloadToFile(String path, File dest, long deadlineMs) {
        HttpURLConnection conn = null;
        try {
            URL url = new URL(joinUrl(config.serverUrl, path));
            conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("GET");
            conn.setConnectTimeout(5000);
            conn.setReadTimeout(120000);
            conn.setRequestProperty("Accept", "application/zip, application/octet-stream, */*");
            if (config.token != null && !config.token.isEmpty()) {
                conn.setRequestProperty("X-Agent-Token", config.token);
            }
            int status = conn.getResponseCode();
            if (status < 200 || status >= 300) {
                // 平台把「为什么不给下」写在响应体里（没打包、包被清理了、越权），
                // 只报状态码的话，机器上完全看不出该去查什么
                throw new RuntimeException("下载失败 HTTP " + status + errorDetail(conn));
            }
            File parent = dest.getParentFile();
            if (parent != null && !parent.exists()) {
                parent.mkdirs();
            }
            try (java.io.InputStream in = conn.getInputStream();
                 java.io.FileOutputStream out = new java.io.FileOutputStream(dest)) {
                byte[] buf = new byte[65536];
                int n;
                long total = 0;
                while ((n = in.read(buf)) > 0) {
                    if (deadlineMs > 0 && System.currentTimeMillis() > deadlineMs) {
                        throw new RuntimeException("下载超时，已中止，避免占死节点");
                    }
                    total += n;
                    if (total > MAX_DOWNLOAD_BYTES) {
                        throw new RuntimeException("下载超过 " + (MAX_DOWNLOAD_BYTES / 1024 / 1024)
                                + " MB，已中止，避免把本机磁盘写满");
                    }
                    out.write(buf, 0, n);
                }
            }
        } catch (Exception e) {
            throw new RuntimeException("GET " + path + " 下载失败: " + e.getMessage(), e);
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    /** 读错误响应里的 message，读不出来就返回空串——它只是用来补充说明，不能反过来盖掉原始错误。 */
    private static String errorDetail(HttpURLConnection conn) {
        try (java.io.InputStream err = conn.getErrorStream()) {
            if (err == null) {
                return "";
            }
            java.io.ByteArrayOutputStream buf = new java.io.ByteArrayOutputStream();
            byte[] tmp = new byte[4096];
            int n;
            while ((n = err.read(tmp)) > 0 && buf.size() < 8192) {
                buf.write(tmp, 0, n);
            }
            String text = new String(buf.toByteArray(), java.nio.charset.StandardCharsets.UTF_8).trim();
            if (text.isEmpty()) {
                return "";
            }
            Object parsed = Json.parse(text);
            if (parsed instanceof java.util.Map) {
                Object msg = ((java.util.Map<?, ?>) parsed).get("message");
                if (msg != null && !String.valueOf(msg).trim().isEmpty()) {
                    return "：" + msg;
                }
            }
            return "：" + (text.length() > 300 ? text.substring(0, 300) : text);
        } catch (Exception ignored) {
            return "";
        }
    }

    private Object request(String method, String path, Object body) {
        return request(method, path, body, 15);
    }

    private Object request(String method, String path, Object body, int readTimeoutSec) {
        HttpURLConnection conn = null;
        try {
            URL url = new URL(joinUrl(config.serverUrl, path));
            conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod(method);
            conn.setConnectTimeout(5000);
            conn.setReadTimeout(readTimeoutSec * 1000);
            conn.setRequestProperty("Accept", "application/json");
            // 带 Agent token 鉴权（注册后 config.token 非空，后续直连接口后端校验）
            if (config.token != null && !config.token.isEmpty()) {
                conn.setRequestProperty("X-Agent-Token", config.token);
            }
            // 接入凭证只在注册时出示，不跟着每个请求到处跑
            if (path.endsWith("/agents/register")
                    && config.enrollToken != null && !config.enrollToken.isEmpty()) {
                conn.setRequestProperty("X-Enroll-Token", config.enrollToken);
            }
            if (body != null) {
                conn.setDoOutput(true);
                conn.setRequestProperty("Content-Type", "application/json");
                byte[] payload = Json.stringify(body).getBytes(StandardCharsets.UTF_8);
                try (OutputStream os = conn.getOutputStream()) {
                    os.write(payload);
                }
            }
            int status = conn.getResponseCode();
            java.io.InputStream is = (status >= 200 && status < 300)
                    ? conn.getInputStream() : conn.getErrorStream();
            String resp = readAll(is);
            if (resp == null || resp.trim().isEmpty()) {
                return null;
            }
            Object root = Json.parse(resp);
            int code = Json.integer(Json.obj(root).get("code"));
            if (code != 0) {
                throw new RuntimeException("服务端错误: " + Json.str(Json.obj(root).get("message")));
            }
            return Json.obj(root).get("data");
        } catch (Exception e) {
            throw new RuntimeException(method + " " + path + " 失败: " + e.getMessage(), e);
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    private static String readAll(java.io.InputStream is) throws Exception {
        if (is == null) return "";
        try (java.io.InputStream in = is) {
            byte[] buf = new byte[8192];
            int n;
            java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
            while ((n = in.read(buf)) > 0) {
                if (bos.size() + n > MAX_JSON_BYTES) {
                    throw new RuntimeException("响应超过 8MB，已丢弃，避免撑爆 Agent 内存");
                }
                bos.write(buf, 0, n);
            }
            return new String(bos.toByteArray(), StandardCharsets.UTF_8);
        }
    }
}
