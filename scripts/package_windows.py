"""Keep the Windows batch file and the existing packaging command working."""
import sys

if __package__:
    from .package_desktop import main as package_desktop
else:
    from package_desktop import main as package_desktop


def main():
    if sys.platform != "win32":
        raise SystemExit("请在 Windows 上构建 Windows EXE。")
    package_desktop()


if __name__ == "__main__":
    main()
