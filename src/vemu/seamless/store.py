"""Direct activation of packaged Store/MSIX apps on Windows.

``explorer.exe shell:AppsFolder\\<AUMID>`` is convenient but starts an extra
Explorer process. Windows' own Start menu and Store activate packaged apps
through ``IApplicationActivationManager``. The small helper compiled here calls
that COM interface directly from an interactive scheduled task.
"""

from __future__ import annotations

from ..errors import VemuError
from ..guest.transport import GuestTransport

ACTIVATOR_REMOTE = r"C:\ProgramData\vEMU\guest\vemu-activate.exe"
ACTIVATOR_SOURCE_REMOTE = r"C:\ProgramData\vEMU\guest\vemu-activate.cs"

ACTIVATOR_SOURCE = r"""
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;

[ComImport, Guid("2e941141-7f97-4756-ba1d-9decde894a3d"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IApplicationActivationManager
{
    [PreserveSig]
    int ActivateApplication(
        [MarshalAs(UnmanagedType.LPWStr)] string appUserModelId,
        [MarshalAs(UnmanagedType.LPWStr)] string arguments,
        int options,
        out uint processId);
}

[ComImport, Guid("45BA127D-10A8-46EA-8AB7-56EA9078943C")]
public class ApplicationActivationManager
{
}

public static class Program
{
    private const int ActivationOptions = 0;

    [STAThread]
    public static int Main(string[] args)
    {
        if (args.Length < 1 || string.IsNullOrWhiteSpace(args[0]))
            return 2;

        string aumid = args[0].Trim();
        const string prefix = "shell:AppsFolder\\";
        if (aumid.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            aumid = aumid.Substring(prefix.Length);

        try
        {
            var manager =
                (IApplicationActivationManager)new ApplicationActivationManager();
            uint processId;
            int hr = manager.ActivateApplication(
                aumid,
                args.Length > 1 ? args[1] : "",
                ActivationOptions,
                out processId);
            if (hr < 0)
            {
                Marshal.ThrowExceptionForHR(hr);
                return 1;
            }
            Console.WriteLine(processId);
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex.Message);
            return 1;
        }
    }
}
"""


def _escape(value: str) -> str:
    return value.replace("'", "''")


def ensure_activator(transport: GuestTransport) -> str | None:
    """Compile and cache the small COM activation helper on the guest.

    Returns the guest path, or ``None`` when the helper cannot be built. The
    caller should fall back to ``explorer.exe shell:AppsFolder``.
    """
    check = transport.run_powershell(
        f"if (Test-Path -LiteralPath '{_escape(ACTIVATOR_REMOTE)}') {{ "
        f"(Get-Item -LiteralPath '{_escape(ACTIVATOR_REMOTE)}').Length "
        "} else { 0 }",
        timeout=30,
    )
    if check.ok:
        try:
            if int(check.output.strip().splitlines()[-1]) > 0:
                return ACTIVATOR_REMOTE
        except (ValueError, IndexError):
            pass

    try:
        transport.upload(
            ACTIVATOR_SOURCE.encode("utf-8"),
            ACTIVATOR_SOURCE_REMOTE,
        )
    except VemuError:
        return None

    script = f"""
$ErrorActionPreference = 'Stop'
$source = Get-Content -LiteralPath '{_escape(ACTIVATOR_SOURCE_REMOTE)}' -Raw
Add-Type -TypeDefinition $source -Language CSharp `
  -OutputAssembly '{_escape(ACTIVATOR_REMOTE)}' `
  -OutputType WindowsApplication
if (-not (Test-Path -LiteralPath '{_escape(ACTIVATOR_REMOTE)}')) {{ throw 'helper not created' }}
(Get-Item -LiteralPath '{_escape(ACTIVATOR_REMOTE)}').Length
"""
    try:
        result = transport.run_powershell(script, timeout=120)
    except VemuError:
        return None
    if not result.ok:
        return None
    try:
        size = int(result.output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return ACTIVATOR_REMOTE if size > 0 else None
