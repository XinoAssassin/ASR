; NSIS hooks of the "desktop + backend" installer (tauri.bundled.conf.json).
;
; Uninstall removes the managed Python environment (%LOCALAPPDATA%\dev.asr.recorder\backend:
; source copies + venvs, uv cache, uv-managed Python) but never the backend's data
; (%LOCALAPPDATA%\dev.asr.recorder\data: asr.db, uploads) or the models folder
; (%LOCALAPPDATA%\dev.asr.recorder\models). Tauri's "delete app data" checkbox removes the whole
; %LOCALAPPDATA%\dev.asr.recorder folder, so data and models are moved aside before that and
; put back afterwards. Folders chosen elsewhere in the setup wizard are never touched.
; Updates (the uninstaller run with /UPDATE by a newer installer) keep everything: the app
; re-syncs the environment at its next start.
; Portable builds do not use an installer: delete the folder instead.

!macro NSIS_HOOK_PREUNINSTALL
  ${If} $UpdateMode <> 1
    SetShellVarContext current
    ; env-1\ env-2\ (source + .venv), uv-cache\, python\: nothing else lives there
    RmDir /r "$LOCALAPPDATA\${BUNDLEID}\backend"
    ${If} $DeleteAppDataCheckboxState = 1
      ${If} ${FileExists} "$LOCALAPPDATA\${BUNDLEID}\data\*.*"
        Rename "$LOCALAPPDATA\${BUNDLEID}\data" "$LOCALAPPDATA\${BUNDLEID}.keep-data"
      ${EndIf}
      ${If} ${FileExists} "$LOCALAPPDATA\${BUNDLEID}\models\*.*"
        Rename "$LOCALAPPDATA\${BUNDLEID}\models" "$LOCALAPPDATA\${BUNDLEID}.keep-models"
      ${EndIf}
    ${EndIf}
  ${EndIf}
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $UpdateMode <> 1
    SetShellVarContext current
    ${If} ${FileExists} "$LOCALAPPDATA\${BUNDLEID}.keep-data\*.*"
      CreateDirectory "$LOCALAPPDATA\${BUNDLEID}"
      Rename "$LOCALAPPDATA\${BUNDLEID}.keep-data" "$LOCALAPPDATA\${BUNDLEID}\data"
    ${EndIf}
    ${If} ${FileExists} "$LOCALAPPDATA\${BUNDLEID}.keep-models\*.*"
      CreateDirectory "$LOCALAPPDATA\${BUNDLEID}"
      Rename "$LOCALAPPDATA\${BUNDLEID}.keep-models" "$LOCALAPPDATA\${BUNDLEID}\models"
    ${EndIf}
  ${EndIf}
!macroend
