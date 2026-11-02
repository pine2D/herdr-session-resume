#!/usr/bin/env bash
# herdr-resume 安装/卸载：只创建指向本仓库的符号链接并启用用户服务，不改 shell 配置。
set -euo pipefail

# 界面语言：与 herdr_resume.py 的 lang() 规则一致
is_zh() {
    local v="${HERDR_RESUME_LANG:-}"
    if [[ -z "$v" ]]; then
        v="${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}"
        if [[ -n "${LANGUAGE:-}" && -n "$v" && "$v" != C && "$v" != POSIX && "$v" != C.* ]]; then
            v="${LANGUAGE%%:*}"
        fi
    fi
    [[ "${v,,}" == zh* ]]
}

# msg <英文> <中文>
msg() {
    if is_zh; then printf '%s\n' "$2"; else printf '%s\n' "$1"; fi
}

usage() {
    if is_zh; then
        cat <<'EOF'
install.sh — 安装或卸载 herdr-resume

用法：
  ./install.sh              安装：链接命令和用户服务，启用并启动服务
  ./install.sh --uninstall  卸载：停用服务，删除本仓库创建的链接（保留状态目录）
  ./install.sh --dry-run    只打印将执行的操作
  ./install.sh --help

安装后需自行在 ~/.zshrc 末尾加一行（脚本会打印具体内容）。

示例：
  ./install.sh --dry-run && ./install.sh

副作用：
  创建 ~/.local/bin/herdr-resume 与 ~/.config/systemd/user/herdr-resume.service 两个符号链接，
  执行 systemctl --user daemon-reload / enable / restart。已存在且不指向本仓库的同名文件不会被覆盖。

退出状态：
  0  成功；1  依赖缺失或目标被占用；2  参数错误。
EOF
    else
        cat <<'EOF'
install.sh — install or uninstall herdr-resume

Usage:
  ./install.sh              install: link the command and user service, then enable and start it
  ./install.sh --uninstall  uninstall: stop the service and remove links this repo created (state is kept)
  ./install.sh --dry-run    only print what would be done
  ./install.sh --help

After installing, add one line to the end of ~/.zshrc (the script prints it).

Examples:
  ./install.sh --dry-run && ./install.sh

Side effects:
  Creates two symlinks, ~/.local/bin/herdr-resume and ~/.config/systemd/user/herdr-resume.service,
  and runs systemctl --user daemon-reload / enable / restart. Existing files with the same name that
  do not point into this repo are never overwritten.

Exit status:
  0  success; 1  missing dependency or target already taken; 2  usage error.
EOF
    fi
}

dry_run=false
action=install
for arg in "$@"; do
    case "$arg" in
        -h|--help) (( $# == 1 )) || exit 2; usage; exit 0 ;;
        --uninstall) action=uninstall ;;
        --dry-run) dry_run=true ;;
        *) msg "install.sh: unknown option $arg" "install.sh: 未知参数 $arg" >&2; exit 2 ;;
    esac
done

repo=$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd)
bin_link="$HOME/.local/bin/herdr-resume"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit_link="$unit_dir/herdr-resume.service"
zshrc_line="[[ -r $repo/herdr-resume.zsh ]] && source $repo/herdr-resume.zsh"

run() {
    printf '+ %s\n' "$*"
    "$dry_run" || "$@"
}

ours() {
    [[ -L "$1" && "$(readlink -f -- "$1")" == "$2" ]]
}

link() {
    local target="$1" path="$2"
    if [[ -e "$path" || -L "$path" ]] && ! ours "$path" "$target"; then
        msg "install.sh: $path already exists and does not point into this repo; move it away first" \
            "install.sh: $path 已存在且不指向本仓库，请先自行处理" >&2
        exit 1
    fi
    run mkdir -p -- "$(dirname -- "$path")"
    run ln -sfn -- "$target" "$path"
}

unlink_ours() {
    local target="$1" path="$2"
    if ours "$path" "$target"; then
        run rm -f -- "$path"
    elif [[ -e "$path" || -L "$path" ]]; then
        msg "Keeping $path (it does not point into this repo)" "保留 $path（不指向本仓库）"
    fi
}

if [[ "$action" == uninstall ]]; then
    run systemctl --user disable --now herdr-resume.service || true
    unlink_ours "$repo/herdr-resume.service" "$unit_link"
    unlink_ours "$repo/herdr-resume" "$bin_link"
    run systemctl --user daemon-reload
    echo
    msg "Uninstalled. Remove this line from ~/.zshrc:" "已卸载。请从 ~/.zshrc 删除这一行："
    printf '\n  %s\n\n' "$zshrc_line"
    msg "The state directory ~/.local/state/herdr-resume was kept; delete it by hand if you like." \
        "状态目录 ~/.local/state/herdr-resume 已保留，可手动删除。"
    exit 0
fi

missing=()
for cmd in hc zsh systemctl; do
    command -v "$cmd" >/dev/null || missing+=("$cmd")
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null || missing+=("python3>=3.8")
if (( ${#missing[@]} )); then
    msg "install.sh: missing dependencies: ${missing[*]}" "install.sh: 缺少依赖：${missing[*]}" >&2
    exit 1
fi
if ! systemctl --user show-environment >/dev/null 2>&1; then
    msg "install.sh: no systemd user session (on WSL, set [boot] systemd=true in /etc/wsl.conf)" \
        "install.sh: systemd 用户会话不可用（WSL 需在 /etc/wsl.conf 设置 [boot] systemd=true）" >&2
    exit 1
fi

link "$repo/herdr-resume" "$bin_link"
link "$repo/herdr-resume.service" "$unit_link"
# 不用 reenable：其 disable 步骤会删掉刚建的单元链接。旧位置留下的 wants 链接先清掉。
wants="$unit_dir/default.target.wants/herdr-resume.service"
if [[ -L "$wants" ]] && ! ours "$wants" "$repo/herdr-resume.service"; then
    run rm -f -- "$wants"
fi
run systemctl --user daemon-reload
run systemctl --user enable herdr-resume.service
run systemctl --user restart herdr-resume.service

echo
msg "Installed. Last step: add this line to the end of ~/.zshrc (after other plugins):" \
    "已安装。最后一步：在 ~/.zshrc 末尾（其他插件之后）加入："
printf '\n  %s\n\n' "$zshrc_line"
msg "Then run herdr-resume status to see recorded sessions." "然后用 herdr-resume status 查看已记录的会话。"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo; msg "Note: ~/.local/bin is not in PATH; call herdr-resume by its full path." \
                 "注意：~/.local/bin 不在 PATH 中，herdr-resume 命令需用完整路径调用。" ;;
esac
