using System;
using System.Diagnostics;
using System.IO;

var root = AppContext.BaseDirectory;
var launcher = Path.Combine(root, "run_ru_overlay.vbs");
var wscript = Path.Combine(Environment.SystemDirectory, "wscript.exe");
var runtime = Path.Combine(root, "runtime");
var log = Path.Combine(runtime, "launcher.log");

try
{
    Directory.CreateDirectory(runtime);
    File.AppendAllText(log, $"{DateTime.Now:O} root={root} launcher={launcher}{Environment.NewLine}");
    if (!File.Exists(launcher) || !File.Exists(wscript))
    {
        File.AppendAllText(log, "launcher or wscript not found" + Environment.NewLine);
        return;
    }

    var process = Process.Start(new ProcessStartInfo
    {
        FileName = wscript,
        Arguments = $"\"{launcher}\"",
        WorkingDirectory = root,
        UseShellExecute = false,
        CreateNoWindow = true,
        WindowStyle = ProcessWindowStyle.Hidden,
    });
    File.AppendAllText(log, $"wscript PID={process?.Id}{Environment.NewLine}");
}
catch (Exception ex)
{
    try { File.AppendAllText(log, $"ERROR {ex}{Environment.NewLine}"); } catch { }
}
