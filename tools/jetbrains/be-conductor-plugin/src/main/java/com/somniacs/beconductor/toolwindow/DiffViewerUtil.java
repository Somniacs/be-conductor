package com.somniacs.beconductor.toolwindow;

import com.intellij.diff.DiffContentFactory;
import com.intellij.diff.DiffDialogHints;
import com.intellij.diff.DiffManager;
import com.intellij.diff.chains.SimpleDiffRequestChain;
import com.intellij.diff.contents.DocumentContent;
import com.intellij.diff.requests.SimpleDiffRequest;
import com.intellij.openapi.fileTypes.FileType;
import com.intellij.openapi.fileTypes.FileTypeManager;
import com.intellij.openapi.project.Project;
import com.somniacs.beconductor.api.ApiModels;

import java.util.ArrayList;
import java.util.List;

/**
 * Shared utility for opening IntelliJ's native diff viewer
 * from a list of per-file base/head content pairs.
 * <p>
 * Must be called on the EDT.
 */
public final class DiffViewerUtil {

    private DiffViewerUtil() {}

    public static void showDiff(Project project, String title, List<ApiModels.FileDiff> files) {
        DiffContentFactory factory = DiffContentFactory.getInstance();
        List<SimpleDiffRequest> requests = new ArrayList<>();

        for (ApiModels.FileDiff file : files) {
            FileType fileType = FileTypeManager.getInstance()
                    .getFileTypeByFileName(file.path);
            String baseText = file.base_content != null ? file.base_content : "";
            String headText = file.head_content != null ? file.head_content : "";

            DocumentContent baseContent = factory.create(project, baseText, fileType);
            DocumentContent headContent = factory.create(project, headText, fileType);

            String requestTitle = file.status + ": " + file.path;
            SimpleDiffRequest request = new SimpleDiffRequest(
                    requestTitle, baseContent, headContent,
                    "Base", "Head"
            );
            requests.add(request);
        }

        if (requests.size() == 1) {
            DiffManager.getInstance().showDiff(project, requests.get(0));
        } else {
            SimpleDiffRequestChain chain = new SimpleDiffRequestChain(requests);
            DiffManager.getInstance().showDiff(project, chain, DiffDialogHints.FRAME);
        }
    }
}
