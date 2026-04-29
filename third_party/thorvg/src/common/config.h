#ifndef THORVG_CONFIG_H
#define THORVG_CONFIG_H

#define THORVG_CPU_ENGINE_SUPPORT
#define THORVG_VERSION_STRING "1.0.4"

// 전역 new/delete 오버라이드 비활성화 (openpilot/Qt와 충돌 방지)
#define TVG_CUSTOM_ALLOCATOR_DISABLE

#endif
