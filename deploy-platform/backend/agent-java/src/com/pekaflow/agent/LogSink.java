package com.pekaflow.agent;

/** 日志出口：执行器只管往里写，攒批和上报由 LogBatcher 负责。 */
public interface LogSink {
    void log(String line);
}
