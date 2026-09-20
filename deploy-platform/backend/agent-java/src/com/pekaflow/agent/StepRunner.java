package com.pekaflow.agent;

import java.util.Map;

/**
 * 任务执行器的共同接口。
 *
 * 构建机用 TaskExecutor（能跑插件和脚本），生产节点用 NodeExecutor（只做内置发布动作）。
 * AgentMain 只依赖这个接口，两种角色共用同一套领取、取消、上报流程。
 */
public interface StepRunner {

    boolean executeTask(Map<String, Object> task, LogSink sink) throws Exception;

    void notifyCancelled(String reason);

    /** 构建机会返回本次拉到的代码版本；节点没有代码，返回 null。 */
    String getLastSourceRef();
}
