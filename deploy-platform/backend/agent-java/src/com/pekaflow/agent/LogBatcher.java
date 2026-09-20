package com.pekaflow.agent;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 按任务聚合日志，定时/定量批量上报（降低 HTTP QPS）。
 *
 * 平台挂了时：
 *  - 缓冲封顶，超了丢最旧的，避免撑爆本机内存
 *  - 失败后冷却再试，避免每来一行就序列化整份缓冲打满 CPU
 *  - 每次只送一小截，平台恢复后也不会一次 POST 几千行被接口截掉
 */
public final class LogBatcher {
    static final int MAX_BUFFER = 8000;
    static final int MAX_LINE_CHARS = 4000;
    static final int FLUSH_CHUNK = 200;

    interface Poster {
        void send(List<String> lines) throws Exception;
    }

    private final Poster poster;
    private final Object lock = new Object();
    private final ArrayDeque<String> buffer = new ArrayDeque<String>();
    private long lastFlushMs = System.currentTimeMillis();
    private long skipFlushUntil;
    private int failStreak;
    private int dropped;
    private final Thread flusher;
    private volatile boolean stopped = false;

    public LogBatcher(ApiClient api, int agentId, int taskId) {
        this(apiPoster(api, agentId, taskId), true);
    }

    LogBatcher(Poster poster, boolean startThread) {
        this.poster = poster;
        if (startThread) {
            flusher = new Thread(this::loop, "log-batch");
            flusher.setDaemon(true);
            flusher.start();
        } else {
            flusher = null;
        }
    }

    private static Poster apiPoster(final ApiClient api, final int agentId, final int taskId) {
        return new Poster() {
            public void send(List<String> lines) throws Exception {
                Map<String, Object> body = new HashMap<String, Object>();
                body.put("lines", lines);
                api.post("/api/v1/agents/" + agentId + "/tasks/" + taskId + "/log/batch", body);
            }
        };
    }

    public void append(String line) {
        if (line == null) {
            return;
        }
        if (line.length() > MAX_LINE_CHARS) {
            line = line.substring(0, MAX_LINE_CHARS) + "…（单行过长已截断）";
        }
        synchronized (lock) {
            if (buffer.size() >= MAX_BUFFER) {
                buffer.removeFirst();
                dropped++;
            }
            buffer.addLast(line);
            if (buffer.size() >= FLUSH_CHUNK && !cooling()) {
                flushLocked();
            }
        }
    }

    public void flush() {
        synchronized (lock) {
            flushLocked();
        }
    }

    public void close() {
        stopped = true;
        synchronized (lock) {
            if (dropped > 0) {
                buffer.addFirst("[ERROR]: 日志上报积压，已丢弃 " + dropped + " 行（最早的输出已丢失）");
            }
        }
        flush();
        if (flusher != null) {
            flusher.interrupt();
            try {
                flusher.join(2000);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
        }
    }

    int buffered() {
        synchronized (lock) {
            return buffer.size();
        }
    }

    int dropped() {
        synchronized (lock) {
            return dropped;
        }
    }

    private void loop() {
        while (!stopped && !Thread.currentThread().isInterrupted()) {
            try {
                Thread.sleep(100);
            } catch (InterruptedException e) {
                return;
            }
            synchronized (lock) {
                if (!buffer.isEmpty() && !cooling()
                        && System.currentTimeMillis() - lastFlushMs >= 100) {
                    flushLocked();
                }
            }
        }
    }

    private boolean cooling() {
        return !stopped && System.currentTimeMillis() < skipFlushUntil;
    }

    private void flushLocked() {
        if (buffer.isEmpty() || cooling()) {
            return;
        }
        int n = Math.min(FLUSH_CHUNK, buffer.size());
        List<String> batch = new ArrayList<String>(n);
        for (int i = 0; i < n; i++) {
            batch.add(buffer.removeFirst());
        }
        lastFlushMs = System.currentTimeMillis();
        try {
            poster.send(batch);
            failStreak = 0;
            skipFlushUntil = 0;
        } catch (Exception e) {
            for (int i = batch.size() - 1; i >= 0; i--) {
                if (buffer.size() >= MAX_BUFFER) {
                    buffer.removeLast();
                    dropped++;
                }
                buffer.addFirst(batch.get(i));
            }
            failStreak++;
            long wait = Math.min(15_000L, 2_000L << Math.min(failStreak - 1, 3));
            skipFlushUntil = System.currentTimeMillis() + wait;
            System.err.println("[agent] 批量日志上报失败: " + e.getMessage()
                    + "，" + (wait / 1000) + " 秒后再试"
                    + (dropped > 0 ? "（已丢弃 " + dropped + " 行，避免撑爆本机内存）" : ""));
        }
    }
}
