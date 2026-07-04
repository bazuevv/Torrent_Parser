#!/bin/bash
# PreToolUse hook. Проверяет наличие marker-файла для текущей сессии.
# Если marker есть — возвращает JSON с permissionDecision=allow, и harness
# пропускает tool-вызов без окна подтверждения.
#
# Marker создаётся хуком bypass-magic-word.py при получении слова 'да!'
# в UserPromptSubmit и удаляется хуком bypass-cleanup.py при SessionEnd.
#
# Shell + jq выбраны ради скорости: хук фаерится на КАЖДЫЙ tool-вызов,
# Python добавил бы ~80ms латенси.

input=$(cat)
session_id=$(printf '%s' "$input" | jq -r '.session_id // empty')

[ -z "$session_id" ] && exit 0
[ -z "$CLAUDE_PROJECT_DIR" ] && exit 0

marker="$CLAUDE_PROJECT_DIR/.claude/hooks-runtime/$session_id"

if [ -f "$marker" ]; then
    jq -nc --arg sid "$session_id" '{
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "allow",
        permissionDecisionReason: ("bypass active for session " + $sid)
      }
    }'
fi

exit 0
