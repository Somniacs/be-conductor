package com.somniacs.beconductor.agent;

import com.intellij.openapi.fileEditor.FileEditor;
import com.intellij.openapi.fileEditor.FileEditorPolicy;
import com.intellij.openapi.fileEditor.FileEditorProvider;
import com.intellij.openapi.project.DumbAware;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.util.Key;
import com.intellij.openapi.vfs.VirtualFile;
import org.jetbrains.annotations.NonNls;
import org.jetbrains.annotations.NotNull;

/**
 * Opens .agent virtual files as JCEF-backed agent session editors.
 * Registered in plugin.xml as a fileEditorProvider.
 */
public class AgentFileEditorProvider implements FileEditorProvider, DumbAware {

    public static final Key<String> AGENT_URL_KEY = Key.create("be-conductor.agentUrl");
    public static final Key<String> AGENT_SESSION_KEY = Key.create("be-conductor.agentSession");

    @Override
    public boolean accept(@NotNull Project project, @NotNull VirtualFile file) {
        return file.getUserData(AGENT_URL_KEY) != null;
    }

    @Override
    public @NotNull FileEditor createEditor(@NotNull Project project, @NotNull VirtualFile file) {
        String url = file.getUserData(AGENT_URL_KEY);
        String sessionId = file.getUserData(AGENT_SESSION_KEY);
        return new AgentFileEditor(sessionId != null ? sessionId : file.getName(), url != null ? url : "about:blank");
    }

    @Override
    public @NotNull @NonNls String getEditorTypeId() {
        return "be-conductor-agent";
    }

    @Override
    public @NotNull FileEditorPolicy getPolicy() {
        return FileEditorPolicy.HIDE_DEFAULT_EDITOR;
    }
}
