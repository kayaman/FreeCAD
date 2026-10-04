# SPDX-License-Identifier: LGPL-2.1-or-later
set(AIAssistant_Scripts
    Init.py
    InitGui.py
    freecad_ai/__init__.py
    freecad_ai/core.py
    freecad_ai/document.py
    freecad_ai/execution.py
    freecad_ai/gui.py
    freecad_ai/processes.py
    freecad_ai/providers.py
    freecad_ai/session.py
    freecad_ai/validation.py
    freecad_ai/worker.py
)

add_custom_target(AIAssistantScripts ALL SOURCES ${AIAssistant_Scripts})
fc_target_copy_resource(AIAssistantScripts
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_BINARY_DIR}/Mod/AIAssistant
    ${AIAssistant_Scripts})

install(FILES Init.py InitGui.py DESTINATION Mod/AIAssistant)
install(FILES
    freecad_ai/__init__.py
    freecad_ai/core.py
    freecad_ai/document.py
    freecad_ai/execution.py
    freecad_ai/gui.py
    freecad_ai/processes.py
    freecad_ai/providers.py
    freecad_ai/session.py
    freecad_ai/validation.py
    freecad_ai/worker.py
    DESTINATION Mod/AIAssistant/freecad_ai)
