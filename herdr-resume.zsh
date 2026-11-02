# herdr-resume：herdr 终端被恢复（herdr-core 停止后重启、WSL 关机、Windows 重启）后，
# 在新 shell 第一次出提示符前续上原来的 agent 会话。由 ~/.zshrc 末尾 source。
# 非交互 shell、非 herdr 终端直接返回；重复 source 无副作用。
[[ -o interactive && -n ${HC_TERMINAL_ID-} ]] || return 0
(( ${+functions[_herdr_resume_precmd]} )) && return 0

typeset -g _herdr_resume_first=1 _herdr_resume_pending=
typeset -g _herdr_resume_file="${HERDR_RESUME_STATE:-$HOME/.local/state/herdr-resume}/${HC_SESSION:-default}/${HC_TERMINAL_ID}.json"

_herdr_resume_precmd() {
  if (( ! _herdr_resume_first )); then
    # 提示符回来说明本 shell 启动的 agent 已退出：属于本 shell 的条目立即作废，
    # 不等下一轮快照，避免主动退出后马上重启又被拉起。
    [[ -e $_herdr_resume_file && "$(<$_herdr_resume_file)" == *"\"shell_pid\": $$,"* ]] \
      && command rm -f -- $_herdr_resume_file
    return 0
  fi
  _herdr_resume_first=0
  [[ ${HERDR_RESUME_MODE:-auto} != off && -e $_herdr_resume_file ]] || return 0

  local -a r
  # claim 输出 4 段，以 NUL 分隔（目录名含换行也不会错位）
  r=("${(@0)$("${HERDR_RESUME_BIN:-$HOME/.local/bin/herdr-resume}" claim \
      --terminal "$HC_TERMINAL_ID" --shell-pid $$ 2>/dev/null)}")
  (( ${#r} == 4 )) || return 0
  local desc=$r[1] dir=$r[2] cmd=$r[3] how=$r[4] key=
  [[ ${HERDR_RESUME_MODE:-auto} == confirm ]] && how=confirm
  if ! builtin cd -q -- "$dir" 2>/dev/null; then
    print -u2 -r -- "[herdr-resume] 目录已不存在：$dir"
    how=confirm
  fi
  if [[ $how == confirm ]]; then
    print -P -- "%F{cyan}[herdr-resume]%f ${desc//\%/%%} —— 命令已填入，Enter 执行，Ctrl-U 放弃"
    print -z -r -- "$cmd"
    return 0
  fi
  local delay=${HERDR_RESUME_DELAY:-3}
  print -nP -- "%F{cyan}[herdr-resume]%f ${desc//\%/%%} —— ${delay} 秒后恢复（Enter 立即，n/Esc 跳过）"
  read -s -t "$delay" -k 1 key
  print
  if [[ $key == [nN] || $key == $'\e' ]]; then
    command rm -f -- $_herdr_resume_file
    print -s -r -- "$cmd"
    print -r -- "[herdr-resume] 已跳过；恢复命令在历史里（↑）"
    return 0
  fi
  _herdr_resume_pending=$cmd
}

# 经 zle 提交，命令像手敲的一样进历史、触发 preexec（标题、herdr 状态）。
_herdr_resume_line_init() {
  [[ -n $_herdr_resume_pending ]] || return 0
  BUFFER=$_herdr_resume_pending
  _herdr_resume_pending=
  zle .accept-line
}

autoload -Uz add-zsh-hook add-zle-hook-widget
add-zsh-hook precmd _herdr_resume_precmd
add-zle-hook-widget line-init _herdr_resume_line_init
