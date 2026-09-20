package com.example;

import com.pekaflow.atom.ReleaseAtomSdk;

/** 示例入口：javac 后随包分发 class。 */
public class HelloAtom {
    public static void main(String[] args) {
        String input = ReleaseAtomSdk.getInputJson();
        ReleaseAtomSdk.logInfo("workspace=" + ReleaseAtomSdk.getWorkspace());
        ReleaseAtomSdk.logInfo("input=" + input);
        ReleaseAtomSdk.setOutput(ReleaseAtomSdk.SUCCESS, "ok", ReleaseAtomSdk.mapOf("echo", input));
    }
}
