package com.pekaflow.agent;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

/** 不进 jar。编译：javac -cp out test/LogBatcherTest.java -d out && java -cp out com.pekaflow.agent.LogBatcherTest */
public final class LogBatcherTest {
    private static int failed;

    public static void main(String[] args) {
        testCapsBufferAndDropsOldest();
        testDoesNotFlushEveryLineWhenDown();
        testSendsInChunksNotTheWholeBuffer();
        testTruncatesLongLine();
        if (failed > 0) {
            System.err.println("FAILED " + failed);
            System.exit(1);
        }
        System.out.println("LogBatcherTest OK");
    }

    private static void testCapsBufferAndDropsOldest() {
        AtomicInteger posts = new AtomicInteger();
        LogBatcher b = new LogBatcher(new LogBatcher.Poster() {
            public void send(List<String> lines) {
                posts.incrementAndGet();
                throw new RuntimeException("down");
            }
        }, false);
        for (int i = 0; i < 9000; i++) {
            b.append("L" + i);
        }
        check("buffer cap", b.buffered() <= LogBatcher.MAX_BUFFER);
        check("dropped some", b.dropped() >= 9000 - LogBatcher.MAX_BUFFER);
        check("not a post-per-line storm", posts.get() <= 5);
        b.close();
    }

    private static void testDoesNotFlushEveryLineWhenDown() {
        AtomicInteger posts = new AtomicInteger();
        LogBatcher b = new LogBatcher(new LogBatcher.Poster() {
            public void send(List<String> lines) {
                posts.incrementAndGet();
                throw new RuntimeException("down");
            }
        }, false);
        for (int i = 0; i < 400; i++) {
            b.append("x" + i);
        }
        int afterBurst = posts.get();
        check("first flush happened", afterBurst >= 1);
        check("cooldown stops the storm", afterBurst <= 2);
        b.close();
    }

    private static void testSendsInChunksNotTheWholeBuffer() {
        final List<Integer> sizes = new ArrayList<Integer>();
        LogBatcher b = new LogBatcher(new LogBatcher.Poster() {
            public void send(List<String> lines) {
                sizes.add(lines.size());
            }
        }, false);
        for (int i = 0; i < 500; i++) {
            b.append("ok" + i);
        }
        check("chunked", !sizes.isEmpty());
        for (Integer n : sizes) {
            check("chunk <= FLUSH_CHUNK", n <= LogBatcher.FLUSH_CHUNK);
        }
        b.flush();
        check("drained", b.buffered() == 0);
        b.close();
    }

    private static void testTruncatesLongLine() {
        final List<String> seen = new ArrayList<String>();
        LogBatcher b = new LogBatcher(new LogBatcher.Poster() {
            public void send(List<String> lines) {
                seen.addAll(lines);
            }
        }, false);
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 6000; i++) {
            sb.append('a');
        }
        b.append(sb.toString());
        b.flush();
        check("got line", seen.size() == 1);
        check("truncated", seen.get(0).length() < 5000);
        b.close();
    }

    private static void check(String name, boolean ok) {
        if (!ok) {
            failed++;
            System.err.println("  FAIL " + name);
        }
    }
}
