// ThorVG config.h — SCons 빌드용 수동 설정
// 원래 meson 빌드에서 자동 생성되는 파일

#ifndef THORVG_CONFIG_H
#define THORVG_CONFIG_H

// 엔진 활성화
#define THORVG_SW_RASTER_SUPPORT

// 파일 I/O (tvgLoaderMgr.cpp에서 필요)
// 비활성화: 파일 로딩 불필요 (도형만 사용)
// #define THORVG_FILE_IO_SUPPORT

// 비활성화 (불필요한 로더/기능)
// #define THORVG_GL_RASTER_SUPPORT
// #define THORVG_WG_RASTER_SUPPORT
// #define THORVG_SVG_LOADER_SUPPORT
// #define THORVG_PNG_LOADER_SUPPORT
// #define THORVG_JPG_LOADER_SUPPORT
// #define THORVG_WEBP_LOADER_SUPPORT
// #define THORVG_LOTTIE_LOADER_SUPPORT
// #define THORVG_TTF_LOADER_SUPPORT
// #define THORVG_LOG_ENABLED

// 버전 정보
#define THORVG_VERSION_STRING "1.0.4"

#endif // THORVG_CONFIG_H
