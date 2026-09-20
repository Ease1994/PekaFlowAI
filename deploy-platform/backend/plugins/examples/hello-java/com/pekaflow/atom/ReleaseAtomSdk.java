package com.pekaflow.atom;

import java.io.File;
import java.io.FileWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * release Atom SDK（JDK 8，无第三方依赖）。
 * Agent 通过环境变量注入上下文；插件写 stdout 日志与 .release_atom_output.json。
 */
public final class ReleaseAtomSdk {
    private ReleaseAtomSdk() {}

    public static final String SUCCESS = "success";
    public static final String FAILURE = "failure";
    public static final String ERROR = "error";

    public static String getWorkspace() {
        String v = env("RELEASE_WORKSPACE");
        if (v.isEmpty()) v = env("BK_CI_WORKSPACE");
        return v.isEmpty() ? System.getProperty("user.dir") : v;
    }

    public static String getSrc() {
        String v = env("RELEASE_SRC");
        return v.isEmpty() ? getWorkspace() + File.separator + "src" : v;
    }

    /** 插件入参 JSON 原文。 */
    public static String getInputJson() {
        return env("RELEASE_ATOM_INPUT_JSON");
    }

    public static String getPipelineId() {
        return first(env("RELEASE_PIPELINE_ID"), env("BK_CI_PIPELINE_ID"));
    }

    public static String getBuildId() {
        return first(env("RELEASE_BUILD_ID"), env("BK_CI_BUILD_ID"));
    }

    public static String getJobName() {
        return env("RELEASE_JOB_NAME");
    }

    public static String getSensitiveConf(String key) {
        return env("RELEASE_SENSITIVE_" + key.toUpperCase());
    }

    public static void logInfo(String msg) {
        System.out.println("[INFO]: " + msg);
        System.out.flush();
    }

    public static void logError(String msg) {
        System.err.println("[ERROR]: " + msg);
        System.err.flush();
    }

    /**
     * 写输出。data 为字段名 → 字符串值。
     */
    public static void setOutput(String status, String message, Map<String, String> data) {
        StringBuilder json = new StringBuilder();
        json.append("{\"status\":\"").append(esc(status))
                .append("\",\"message\":\"").append(esc(message))
                .append("\",\"type\":\"default\",\"data\":{");
        boolean first = true;
        if (data != null) {
            for (Map.Entry<String, String> e : data.entrySet()) {
                if (!first) json.append(',');
                json.append('"').append(esc(e.getKey())).append("\":{\"type\":\"string\",\"value\":\"")
                        .append(esc(e.getValue())).append("\"}");
                System.out.println("##[set-output]" + e.getKey() + "=" + e.getValue());
                first = false;
            }
        }
        json.append("}}");
        writeFile(new File(getWorkspace(), ".release_atom_output.json"), json.toString());
        writeFile(new File(System.getProperty("user.dir"), ".release_atom_output.json"), json.toString());
    }

    public static Map<String, String> mapOf(String k, String v) {
        Map<String, String> m = new LinkedHashMap<>();
        m.put(k, v);
        return m;
    }

    private static void writeFile(File f, String text) {
        try {
            File p = f.getParentFile();
            if (p != null) p.mkdirs();
            try (FileWriter w = new FileWriter(f)) {
                w.write(text);
            }
        } catch (Exception ignored) {
        }
    }

    private static String env(String k) {
        String v = System.getenv(k);
        return v == null ? "" : v;
    }

    private static String first(String a, String b) {
        return a == null || a.isEmpty() ? (b == null ? "" : b) : a;
    }

    private static String esc(String s) {
        if (s == null) return "";
        return s.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n");
    }
}
