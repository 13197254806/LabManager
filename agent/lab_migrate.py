#!/usr/bin/env python3
"""把以前 root 账号下的存量文件迁移给各成员（仅依赖标准库，需 root 执行）。

流程：
  1. scan   扫描指定目录下属于 root 的条目，生成一张映射表（TSV）
  2. 手工编辑映射表第一列，标明每个条目归谁、怎么处理
  3. apply  按映射表执行（默认只打印命令，加 --yes 才真正执行）

映射表第一列（action）可以填：
  ?            不处理（默认）
  <用户名>      归某位成员：chown 给他。若在 /root 下（其他人进不去），会先移动到 /home/<用户名>/from_root/
  shared       全组可读写：属组改为 lab-users，加 setgid 和默认 ACL，新建文件也自动可共享
  readonly     全组只读：属组改为 lab-users，组可读。若在 /root 下，会先移动到 --legacy-dir

另外：
  mkshared DIR   创建一个全组可读写的共享目录（如 /data/shared）
"""

import argparse
import os
import pwd
import shutil
import subprocess
import sys
import time

GROUP = "lab-users"
DEFAULT_SCAN = ["/root", "/home", "/data", "/mnt", "/workspace"]
SKIP_NAMES = {".bashrc", ".profile", ".bash_history", ".ssh", ".cache", ".viminfo", ".lesshst", ".wget-hsts",
              ".python_history", ".sudo_as_admin_successful", "lost+found", "snap"}


def human(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return "%.0f%s" % (n, unit)
        n /= 1024.0
    return "%.1fP" % n


def du(path, timeout):
    try:
        out = subprocess.run(["du", "-sb", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True, timeout=timeout).stdout
        return human(int(out.split()[0]))
    except Exception:
        return "?"


def cmd_scan(args):
    rows = []
    for base in args.paths:
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            if name in SKIP_NAMES:
                continue
            p = os.path.join(base, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            # /home 下只关心属于 root 的条目；其他目录也只列 root 的
            if st.st_uid != 0:
                continue
            size = "-" if args.no_size else du(p, args.du_timeout)
            mtime = time.strftime("%Y-%m-%d", time.localtime(st.st_mtime))
            note = ""
            if name in ("miniconda3", "anaconda3", "miniforge3", "mambaforge"):
                note = "conda：建议导出环境让成员各自重建，见管理员手册"
            rows.append(("?", p, size, mtime, note))
    out = open(args.output, "w") if args.output else sys.stdout
    out.write("# action\tpath\tsize\tmtime\tnote\n")
    out.write("# action 可填：? / <用户名> / shared / readonly。编辑后执行 apply\n")
    for r in rows:
        out.write("\t".join(r) + "\n")
    if args.output:
        out.close()
        print("已写入 %s，共 %d 项。编辑第一列后执行：%s apply %s" % (args.output, len(rows), sys.argv[0], args.output))


class Runner(object):
    def __init__(self, execute):
        self.execute = execute

    def run(self, *cmd):
        print("  $ " + " ".join("'%s'" % c if " " in c else c for c in cmd))
        if self.execute:
            subprocess.run(cmd, check=True)

    def move(self, src, dst):
        print("  $ mv '%s' '%s'" % (src, dst))
        if self.execute:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)


def make_shared(r, path):
    r.run("chgrp", "-R", GROUP, path)
    r.run("chmod", "-R", "g+rwX", path)
    if os.path.isdir(path):
        r.run("find", path, "-type", "d", "-exec", "chmod", "g+s", "{}", "+")
        if shutil.which("setfacl"):
            r.run("setfacl", "-R", "-m", "g:%s:rwX" % GROUP, "-m", "d:g:%s:rwX" % GROUP, path)
        else:
            print("  [提示] 未安装 acl（apt install acl），新建文件可能需要成员自己 chmod g+w")


def cmd_apply(args):
    r = Runner(args.yes)
    if not args.yes:
        print("【预演模式】以下命令不会执行，确认无误后加 --yes\n")
    with open(args.mapping) as f:
        lines = [l.rstrip("\n").split("\t") for l in f if l.strip() and not l.startswith("#")]
    for cols in lines:
        action, path = cols[0].strip(), cols[1].strip()
        if action in ("", "?", "skip"):
            continue
        if not os.path.lexists(path):
            print("[跳过] 不存在：%s" % path)
            continue
        in_root = os.path.abspath(path).startswith("/root/")
        print("[%s] %s" % (action, path))
        if action == "shared":
            if in_root:
                dst = os.path.join(args.legacy_dir, os.path.basename(path))
                r.move(path, dst)
                path = dst
            make_shared(r, path)
        elif action == "readonly":
            if in_root:
                dst = os.path.join(args.legacy_dir, os.path.basename(path))
                r.move(path, dst)
                path = dst
            r.run("chgrp", "-R", GROUP, path)
            r.run("chmod", "-R", "g+rX,g-w", path)
        else:
            try:
                pw = pwd.getpwnam(action)
            except KeyError:
                print("  [错误] 本机没有用户 %s（成员需先在平台设置用户名，等 Agent 同步后再执行）" % action)
                continue
            if in_root:
                dst = os.path.join(pw.pw_dir, "from_root", os.path.basename(path))
                r.move(path, dst)
                path = dst
                r.run("chown", "%s:%s" % (action, action), os.path.dirname(path))
            r.run("chown", "-R", "%s:%s" % (action, action), path)
    if not args.yes:
        print("\n确认无误后执行：%s apply %s --yes" % (sys.argv[0], args.mapping))


def cmd_mkshared(args):
    r = Runner(True)
    os.makedirs(args.dir, exist_ok=True)
    r.run("chmod", "2775", args.dir)
    make_shared(r, args.dir)
    print("已创建共享目录 %s：%s 组成员均可读写，新建的文件/目录自动继承权限" % (args.dir, GROUP))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("scan", help="扫描 root 拥有的文件，生成映射表")
    s.add_argument("paths", nargs="*", default=DEFAULT_SCAN)
    s.add_argument("-o", "--output", default="migration.tsv")
    s.add_argument("--no-size", action="store_true", help="不统计大小（数据集很大时更快）")
    s.add_argument("--du-timeout", type=int, default=60)
    a = sub.add_parser("apply", help="按映射表执行迁移")
    a.add_argument("mapping")
    a.add_argument("--yes", action="store_true", help="真正执行（默认只预演）")
    a.add_argument("--legacy-dir", default="/data/legacy_root", help="/root 下共享 / 只读条目的移动目标")
    m = sub.add_parser("mkshared", help="创建全组可读写的共享目录")
    m.add_argument("dir")
    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return
    if os.geteuid() != 0 and not (args.cmd == "apply" and not args.yes):
        sys.exit("需要 root 权限")
    {"scan": cmd_scan, "apply": cmd_apply, "mkshared": cmd_mkshared}[args.cmd](args)


if __name__ == "__main__":
    main()
