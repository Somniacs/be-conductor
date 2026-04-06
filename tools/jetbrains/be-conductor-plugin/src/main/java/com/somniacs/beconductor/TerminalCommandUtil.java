package com.somniacs.beconductor;

/**
 * Helpers for composing terminal commands that behave consistently
 * across shells (PowerShell 5.x on Windows, pwsh, bash, zsh, cmd).
 */
public final class TerminalCommandUtil {

    private TerminalCommandUtil() {}

    /**
     * Wraps a command so the terminal closes on success but stays open on failure,
     * using syntax the active shell can parse.
     *
     * <p>Windows PowerShell 5.x (the default on Windows) does not understand {@code &&},
     * so we emit {@code cmd; if ($?) { exit }} there. On non-Windows shells we use
     * the POSIX {@code cmd && exit} form.
     */
    public static String exitOnSuccess(String command) {
        String os = System.getProperty("os.name", "").toLowerCase();
        if (os.contains("win")) {
            return command + "; if ($?) { exit }";
        }
        return command + " && exit";
    }
}
