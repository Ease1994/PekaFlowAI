package com.pekaflow.agent;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 解析/序列化（无第三方依赖，JDK 8 可用）。
 * 支持：对象、数组、字符串、数字、布尔、null。
 */
public final class Json {
    private Json() {}

    // ============ 序列化 ============
    public static String stringify(Object obj) {
        StringBuilder sb = new StringBuilder();
        write(sb, obj);
        return sb.toString();
    }

    private static void write(StringBuilder sb, Object obj) {
        if (obj == null) {
            sb.append("null");
        } else if (obj instanceof String) {
            writeString(sb, (String) obj);
        } else if (obj instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Object e : ((Map<?, ?>) obj).entrySet()) {
                if (!first) sb.append(',');
                Map.Entry<?, ?> entry = (Map.Entry<?, ?>) e;
                writeString(sb, String.valueOf(entry.getKey()));
                sb.append(':');
                write(sb, entry.getValue());
                first = false;
            }
            sb.append('}');
        } else if (obj instanceof List) {
            sb.append('[');
            boolean first = true;
            for (Object item : (List<?>) obj) {
                if (!first) sb.append(',');
                write(sb, item);
                first = false;
            }
            sb.append(']');
        } else if (obj instanceof Boolean || obj instanceof Number) {
            sb.append(obj.toString());
        } else {
            writeString(sb, obj.toString());
        }
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default: sb.append(c);
            }
        }
        sb.append('"');
    }

    // ============ 解析 ============
    public static Object parse(String text) {
        Parser p = new Parser(text);
        Object value = p.parseValue();
        p.skipWhitespace();
        if (!p.isEnd()) {
            throw new RuntimeException("JSON 解析失败：多余字符 at " + p.pos);
        }
        return value;
    }

    private static final class Parser {
        final String text;
        int pos = 0;

        Parser(String text) { this.text = text; }

        boolean isEnd() { return pos >= text.length(); }

        void skipWhitespace() {
            while (pos < text.length() && Character.isWhitespace(text.charAt(pos))) pos++;
        }

        char peek() { return text.charAt(pos); }

        Object parseValue() {
            skipWhitespace();
            if (isEnd()) throw new RuntimeException("JSON 意外结束");
            char c = peek();
            if (c == '{') return parseObject();
            if (c == '[') return parseArray();
            if (c == '"') return parseString();
            if (c == 't') { expect("true"); return Boolean.TRUE; }
            if (c == 'f') { expect("false"); return Boolean.FALSE; }
            if (c == 'n') { expect("null"); return null; }
            return parseNumber();
        }

        void expect(String word) {
            if (!text.startsWith(word, pos)) throw new RuntimeException("JSON 语法错误 at " + pos);
            pos += word.length();
        }

        Map<String, Object> parseObject() {
            pos++; // 跳过 {
            Map<String, Object> map = new LinkedHashMap<>();
            skipWhitespace();
            if (peek() == '}') { pos++; return map; }
            while (true) {
                skipWhitespace();
                String key = parseString();
                skipWhitespace();
                if (peek() != ':') throw new RuntimeException("JSON 期望 : at " + pos);
                pos++;
                map.put(key, parseValue());
                skipWhitespace();
                char c = peek();
                if (c == '}') { pos++; return map; }
                if (c != ',') throw new RuntimeException("JSON 期望 , at " + pos);
                pos++;
            }
        }

        List<Object> parseArray() {
            pos++; // 跳过 [
            List<Object> list = new ArrayList<>();
            skipWhitespace();
            if (peek() == ']') { pos++; return list; }
            while (true) {
                list.add(parseValue());
                skipWhitespace();
                char c = peek();
                if (c == ']') { pos++; return list; }
                if (c != ',') throw new RuntimeException("JSON 期望 , at " + pos);
                pos++;
            }
        }

        String parseString() {
            pos++; // 跳过 "
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (isEnd()) throw new RuntimeException("JSON 字符串未闭合");
                char c = text.charAt(pos++);
                if (c == '"') break;
                if (c == '\\') {
                    char esc = text.charAt(pos++);
                    switch (esc) {
                        case '"': sb.append('"'); break;
                        case '\\': sb.append('\\'); break;
                        case 'n': sb.append('\n'); break;
                        case 'r': sb.append('\r'); break;
                        case 't': sb.append('\t'); break;
                        case 'u':
                            String hex = text.substring(pos, pos + 4);
                            sb.append((char) Integer.parseInt(hex, 16));
                            pos += 4;
                            break;
                        default: sb.append(esc);
                    }
                } else {
                    sb.append(c);
                }
            }
            return sb.toString();
        }

        Object parseNumber() {
            int start = pos;
            while (pos < text.length() && (Character.isDigit(text.charAt(pos))
                    || text.charAt(pos) == '-' || text.charAt(pos) == '.'
                    || text.charAt(pos) == 'e' || text.charAt(pos) == 'E'
                    || text.charAt(pos) == '+')) {
                pos++;
            }
            String num = text.substring(start, pos);
            if (num.contains(".") || num.contains("e") || num.contains("E")) {
                return Double.parseDouble(num);
            }
            try {
                return Long.parseLong(num);
            } catch (NumberFormatException e) {
                return Double.parseDouble(num);
            }
        }
    }

    // ============ 便捷访问 ============
    @SuppressWarnings("unchecked")
    public static Map<String, Object> obj(Object o) {
        return o == null ? new LinkedHashMap<String, Object>() : (Map<String, Object>) o;
    }

    @SuppressWarnings("unchecked")
    public static List<Object> arr(Object o) {
        return o == null ? new ArrayList<Object>() : (List<Object>) o;
    }

    public static String str(Object o) {
        return o == null ? "" : String.valueOf(o);
    }

    public static int integer(Object o) {
        if (o == null) return 0;
        if (o instanceof Number) return ((Number) o).intValue();
        try { return Integer.parseInt(String.valueOf(o)); } catch (Exception e) { return 0; }
    }

    public static boolean bool(Object o) {
        if (o == null) return false;
        if (o instanceof Boolean) return (Boolean) o;
        if (o instanceof Number) return ((Number) o).intValue() != 0;
        String s = String.valueOf(o).trim().toLowerCase();
        return "true".equals(s) || "1".equals(s) || "yes".equals(s);
    }
}
