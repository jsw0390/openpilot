#include "selfdrive/ui/carrot_tvg.h"

#include <thorvg.h>

#ifdef __APPLE__
#include <OpenGL/gl3.h>
#else
#include <GLES3/gl3.h>
#endif

#include <cstring>

#include "common/util.h"

// ─── 상태 ───
static tvg::SwCanvas *tvg_canvas = nullptr;
static uint32_t *tvg_buffer = nullptr;
static int tvg_w = 0, tvg_h = 0;
static bool tvg_initialized = false;

// GL 텍스처 + 쉐이더 (오버레이용)
static GLuint tvg_texture = 0;
static GLuint tvg_vao = 0, tvg_vbo = 0;
static GLuint tvg_shader_program = 0;

// ─── GL 쉐이더 소스 ───
static const char *tvg_vert_src = R"(
#version 300 es
precision mediump float;
layout(location = 0) in vec2 aPos;
layout(location = 1) in vec2 aTexCoord;
out vec2 vTexCoord;
void main() {
    gl_Position = vec4(aPos, 0.0, 1.0);
    vTexCoord = aTexCoord;
}
)";

static const char *tvg_frag_src = R"(
#version 300 es
precision mediump float;
in vec2 vTexCoord;
out vec4 fragColor;
uniform sampler2D uTexture;
void main() {
    fragColor = texture(uTexture, vTexCoord);
}
)";

// ─── GL 초기화 ───
static GLuint compile_shader(GLenum type, const char *src) {
    GLuint shader = glCreateShader(type);
    glShaderSource(shader, 1, &src, nullptr);
    glCompileShader(shader);
    return shader;
}

static void init_gl_overlay(int w, int h) {
    // 쉐이더
    GLuint vs = compile_shader(GL_VERTEX_SHADER, tvg_vert_src);
    GLuint fs = compile_shader(GL_FRAGMENT_SHADER, tvg_frag_src);
    tvg_shader_program = glCreateProgram();
    glAttachShader(tvg_shader_program, vs);
    glAttachShader(tvg_shader_program, fs);
    glLinkProgram(tvg_shader_program);
    glDeleteShader(vs);
    glDeleteShader(fs);

    // 풀스크린 쿼드 (위치 + UV)
    // Y축 뒤집기: ThorVG는 상단=0, GL은 하단=0
    float quad[] = {
        // pos        // uv
        -1.f, -1.f,   0.f, 1.f,
         1.f, -1.f,   1.f, 1.f,
        -1.f,  1.f,   0.f, 0.f,
         1.f,  1.f,   1.f, 0.f,
    };
    glGenVertexArrays(1, &tvg_vao);
    glGenBuffers(1, &tvg_vbo);
    glBindVertexArray(tvg_vao);
    glBindBuffer(GL_ARRAY_BUFFER, tvg_vbo);
    glBufferData(GL_ARRAY_BUFFER, sizeof(quad), quad, GL_STATIC_DRAW);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(float), (void *)0);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(float), (void *)(2 * sizeof(float)));
    glEnableVertexAttribArray(1);
    glBindVertexArray(0);

    // 텍스처
    glGenTextures(1, &tvg_texture);
    glBindTexture(GL_TEXTURE_2D, tvg_texture);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
    glBindTexture(GL_TEXTURE_2D, 0);
}

// ─── ThorVG 초기화 ───
void tvg_init(int w, int h) {
    if (tvg_initialized) return;

    tvg::Initializer::init(0);

    tvg_w = w;
    tvg_h = h;
    tvg_buffer = new uint32_t[w * h];
    memset(tvg_buffer, 0, w * h * sizeof(uint32_t));

    tvg_canvas = tvg::SwCanvas::gen();
    tvg_canvas->target(tvg_buffer, w, w, h, tvg::ColorSpace::ABGR8888S);

    init_gl_overlay(w, h);
    tvg_initialized = true;
}

// ─── ThorVG → GL 텍스처 업로드 ───
static void upload_and_draw(int w, int h) {
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);

    glBindTexture(GL_TEXTURE_2D, tvg_texture);
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, tvg_buffer);

    glUseProgram(tvg_shader_program);
    glBindVertexArray(tvg_vao);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    glBindVertexArray(0);
    glUseProgram(0);
    glBindTexture(GL_TEXTURE_2D, 0);

    glDisable(GL_BLEND);
}

// ─── 매 프레임 렌더링 ───
void tvg_draw(UIState *s, int w, int h) {
    // 초기화 (최초 1회)
    if (!tvg_initialized || tvg_w != w || tvg_h != h) {
        if (tvg_initialized) tvg_destroy();
        tvg_init(w, h);
    }

    // 버퍼 클리어 (투명)
    memset(tvg_buffer, 0, tvg_w * tvg_h * sizeof(uint32_t));

    // 이전 프레임 도형 제거
    tvg_canvas->remove();

    // ── 여기서부터 ThorVG 드로잉 ──

    // 1) 속도 표시 배경 (둥근 사각형)
    float cx = w / 2.0f;
    float box_w = 220, box_h = 130;
    {
        auto bg = tvg::Shape::gen();
        bg->appendRect(cx - box_w / 2, 40, box_w, box_h, 25, 25);
        bg->fill(0, 0, 0, 160);  // 반투명 검정
        tvg_canvas->add(bg);
    }

    // 2) 속도 값 — 상태에 따른 원형 표시
    {
        float speed_r = 45;
        auto circle = tvg::Shape::gen();
        circle->appendCircle(cx, 105, speed_r, speed_r);

        // 속도에 따라 색상 변경
        if (s->status == STATUS_ENGAGED) {
            circle->fill(23, 134, 68, 200);   // 초록 (주행 중)
        } else if (s->status == STATUS_OVERRIDE) {
            circle->fill(145, 155, 149, 200);  // 회색 (오버라이드)
        } else {
            circle->fill(23, 51, 73, 200);     // 남색 (비활성)
        }
        tvg_canvas->add(circle);
    }

    // 3) 상단 상태 바
    {
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
    }

    // 4) "TV" 워터마크 (우하단, 도형으로)
    {
        float tx = w - 120.0f, ty = h - 50.0f;

        // T 글자
        auto t_bar = tvg::Shape::gen();
        t_bar->appendRect(tx, ty, 40, 5, 0, 0);
        t_bar->fill(255, 255, 255, 80);
        tvg_canvas->add(t_bar);

        auto t_stem = tvg::Shape::gen();
        t_stem->appendRect(tx + 17, ty, 6, 25, 0, 0);
        t_stem->fill(255, 255, 255, 80);
        tvg_canvas->add(t_stem);

        // V 글자
        auto v_shape = tvg::Shape::gen();
        v_shape->moveTo(tx + 50, ty);
        v_shape->lineTo(tx + 60, ty + 25);
        v_shape->lineTo(tx + 70, ty);
        v_shape->lineTo(tx + 66, ty);
        v_shape->lineTo(tx + 60, ty + 18);
        v_shape->lineTo(tx + 54, ty);
        v_shape->close();
        v_shape->fill(255, 255, 255, 80);
        tvg_canvas->add(v_shape);
    }

    // ── ThorVG 렌더링 실행 ──
    tvg_canvas->draw(true);
    tvg_canvas->sync();

    // GL 텍스처로 업로드 & 화면에 합성
    upload_and_draw(tvg_w, tvg_h);
}

// ─── 정리 ───
void tvg_destroy() {
    if (tvg_canvas) {
        delete tvg_canvas;
        tvg_canvas = nullptr;
    }
    if (tvg_buffer) {
        delete[] tvg_buffer;
        tvg_buffer = nullptr;
    }
    if (tvg_texture) {
        glDeleteTextures(1, &tvg_texture);
        tvg_texture = 0;
    }
    if (tvg_vao) {
        glDeleteVertexArrays(1, &tvg_vao);
        tvg_vao = 0;
    }
    if (tvg_vbo) {
        glDeleteBuffers(1, &tvg_vbo);
        tvg_vbo = 0;
    }
    if (tvg_shader_program) {
        glDeleteProgram(tvg_shader_program);
        tvg_shader_program = 0;
    }
    tvg::Initializer::term();
    tvg_initialized = false;
}
