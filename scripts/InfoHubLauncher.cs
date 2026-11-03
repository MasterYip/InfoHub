using System;
using System.Diagnostics;
using System.IO;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class InfoHubLauncher
{
    [STAThread]
    private static int Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        try
        {
            string script = Path.Combine(root, "scripts", "start-desktop.ps1");
            if (!File.Exists(script))
                throw new InvalidOperationException("Keep InfoHub.exe in the InfoHub repository folder.");
            if (!File.Exists(Path.Combine(root, ".venv", "Scripts", "python.exe")))
                throw new InvalidOperationException("Run scripts\\setup.ps1 -Desktop in the InfoHub folder first.");
            if (!File.Exists(Path.Combine(root, "config", "hub.local.yaml")))
                throw new InvalidOperationException("Configuration missing. Run scripts\\setup.ps1 -Desktop first.");

            var start = new ProcessStartInfo
            {
                FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),
                                        "WindowsPowerShell", "v1.0", "powershell.exe"),
                Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"" + script + "\"",
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            using (Process process = Process.Start(start))
            {
                Task<string> output = process.StandardOutput.ReadToEndAsync();
                Task<string> error = process.StandardError.ReadToEndAsync();
                process.WaitForExit();
                Task.WaitAll(output, error);
                if (process.ExitCode != 0)
                    throw new InvalidOperationException("InfoHub could not start.\n\n" + error.Result);
            }
            return 0;
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "InfoHub", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return 1;
        }
    }
}
