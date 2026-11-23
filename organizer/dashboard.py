"""Local Windows directory picker. The browser cannot supply the selected path."""
import subprocess


def choose_folder():
    script = """
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object Windows.Forms.FolderBrowserDialog
$d.Description = 'Select the folder for File Organizer'
$d.ShowNewFolderButton = $false
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
if ($d.ShowDialog() -eq 'OK') { [Console]::Write($d.SelectedPath) }
$d.Dispose()
"""
    return subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                          check=True, capture_output=True, encoding="utf-8").stdout
