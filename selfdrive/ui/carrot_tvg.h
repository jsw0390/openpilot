#pragma once

#include "selfdrive/ui/ui.h"

class ModelRenderer;

// ThorVG 기반 HUD 렌더러
void tvg_init(int w, int h);
void tvg_draw(UIState *s, int w, int h, ModelRenderer *model = nullptr);
void tvg_destroy();
