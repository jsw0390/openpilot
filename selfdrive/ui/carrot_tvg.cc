// CarrotPilot ThorVG HUD — GlCanvas GPU 렌더링
// Qt GL 컨텍스트에 직접 렌더링 (CPU 버퍼 불필요)

#include "selfdrive/ui/carrot_tvg.h"
#include <thorvg.h>

#ifdef __APPLE__
#include <OpenGL/gl3.h>
#else
#include <GLES3/gl3.h>
#endif

#include <cstdio>

static tvg::GlCanvas *tvg_canvas = nullptr;
static int tvg_w = 0, tvg_h = 0;
static bool tvg_initialized = false;
static bool tvg_failed = false;

void tvg_init(int w, int h) {
    if (tvg_initialized || tvg_failed) return;

    auto res = tvg::Initializer::init(0);
    if (res != tvg::Result::Success) {
        fprintf(stderr, "[tvg] Initializer::init failed: %d\n", (int)res);
        tvg_failed = true;
        return;
    }

    tvg_canvas = tvg::GlCanvas::gen();
    if (!tvg_canvas) {
        fprintf(stderr, "[tvg] GlCanvas::gen() returned null\n");
        tvg_failed = true;
        return;
    }

    tvg_w = w;
    tvg_h = h;

    // Qt GL 컨텍스트가 이미 current → display/surface/context = nullptr, FBO = 0 (메인 서피스)
    res = tvg_canvas->target(nullptr, nullptr, nullptr, 0, w, h, tvg::ColorSpace::ABGR8888S);
    if (res != tvg::Result::Success) {
        fprintf(stderr, "[tvg] GlCanvas target failed: %d\n", (int)res);
        tvg_failed = true;
        return;
    }

    fprintf(stderr, "[tvg] GlCanvas initialized %dx%d (GPU)\n", w, h);
    tvg_initialized = true;
}

void tvg_draw(UIState *s, int w, int h) {
    if (tvg_failed) return;

    if (!tvg_initialized || tvg_w != w || tvg_h != h) {
        if (tvg_initialized) tvg_destroy();
        tvg_init(w, h);
        if (tvg_failed) return;
    }

    tvg_canvas->remove();

    float cx = w / 2.0f;

    // 속도 배경
    auto bg = tvg::Shape::gen();
    bg->appendRect(cx - 110, 40, 220, 130, 25, 25);
    bg->fill(0, 0, 0, 160);
    tvg_canvas->add(bg);

    // 상태 원
    auto circle = tvg::Shape::gen();
    circle->appendCircle(cx, 105, 45, 45);
    if (s->status == STATUS_ENGAGED) {
        circle->fill(23, 134, 68, 200);
    } else if (s->status == STATUS_OVERRIDE) {
        circle->fill(145, 155, 149, 200);
    } else {
        circle->fill(23, 51, 73, 200);
    }
    tvg_canvas->add(circle);

    // 상단 바
    auto bar = tvg::Shape::gen();
    bar->appendRect(0, 0, (float)w, 4, 0, 0);
    if (s->status == STATUS_ENGAGED) {
        bar->fill(23, 134, 68, 255);
    } else if (s->status == STATUS_OVERRIDE) {
        bar->fill(145, 155, 149, 255);
    } else {
        bar->fill(23, 51, 73, 255);
    }
    tvg_canvas->add(bar);

    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);

    tvg_canvas->draw(true);
    tvg_canvas->sync();

    glDisable(GL_BLEND);
}

void tvg_destroy() {
    if (tvg_canvas) {
        delete tvg_canvas;
        tvg_canvas = nullptr;
    }
    tvg::Initializer::term();
    tvg_initialized = false;
    tvg_failed = false;
}
