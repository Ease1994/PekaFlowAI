package com.pekaflow.agent;

import java.io.PrintStream;
import java.text.SimpleDateFormat;
import java.util.Date;

/**
 * 给 Agent 自身的输出加时间戳。
 *
 * 日志是追加写的，重启不会截断，所以一份 agent.log 里会横跨很多次进程生命周期。
 * 没有时间戳的话，看到一条报错根本判断不出它是刚发生的还是上周留下的。
 *
 * 包装 System.out/err 而不是逐处改 println：调用点几十个，漏一个就又出现没戳的行。
 */
final class AgentLog {

    private AgentLog() {
    }

    static void install() {
        System.setOut(stamp(System.out));
        System.setErr(stamp(System.err));
    }

    private static PrintStream stamp(PrintStream target) {
        return new PrintStream(target, true) {
            private final SimpleDateFormat fmt = new SimpleDateFormat("MM-dd HH:mm:ss");

            @Override
            public void println(String line) {
                synchronized (fmt) {
                    super.println(fmt.format(new Date()) + " " + line);
                }
            }

            @Override
            public void println(Object obj) {
                println(String.valueOf(obj));
            }
        };
    }
}
