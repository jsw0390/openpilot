// CarrotPilot ThorVG HUD — SwCanvas (CPU) + GL 텍스처 오버레이

#include "selfdrive/ui/carrot_tvg.h"
#include <thorvg.h>

#ifdef __APPLE__
#include <OpenGL/gl3.h>
#else
#include <GLES3/gl3.h>
#endif

#include <cstring>
#include <cstdio>

static tvg::SwCanvas *tvg_canvas = nullptr;
static uint32_t *tvg_buffer = nullptr;
static int tvg_w = 0, tvg_h = 0;
static bool tvg_initialized = false;
static bool tvg_failed = false;

static GLuint tvg_texture = 0;
static GLuint tvg_vao = 0, tvg_vbo = 0;
static GLuint tvg_shader_program = 0;

static const char *vert_src = R"(
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

static const char *frag_src = R"(
#version 300 es
precision mediump float;
in vec2 vTexCoord;
out vec4 fragColor;
uniform sampler2D uTexture;
void main() {
    fragColor = texture(uTexture, vTexCoord);
}
)";

static GLuint compile_shader(GLenum type, const char *src) {
    GLuint s = glCreateShader(type);
    glShaderSource(s, 1, &src, nullptr);
    glCompileShader(s);
    GLint ok = 0;
    glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[512];
        glGetShaderInfoLog(s, 512, nullptr, log);
        fprintf(stderr, "[tvg] shader error: %s\n", log);
        glDeleteShader(s);
        return 0;
    }
    return s;
}

static bool init_gl_overlay(int w, int h) {
    GLuint vs = compile_shader(GL_VERTEX_SHADER, vert_src);
    GLuint fs = compile_shader(GL_FRAGMENT_SHADER, frag_src);
    if (!vs || !fs) return false;

    tvg_shader_program = glCreateProgram();
    glAttachShader(tvg_shader_program, vs);
    glAttachShader(tvg_shader_program, fs);
    glLinkProgram(tvg_shader_program);
    glDeleteShader(vs);
    glDeleteShader(fs);

    GLint linked = 0;
    glGetProgramiv(tvg_shader_program, GL_LINK_STATUS, &linked);
    if (!linked) return false;

    float quad[] = {
        -1.f, -1.f, 0.f, 1.f,  1.f, -1.f, 1.f, 1.f,
        -1.f,  1.f, 0.f, 0.f,  1.f,  1.f, 1.f, 0.f,
    };
    glGenVertexArrays(1, &tvg_vao);
    glGenBuffers(1, &tvg_vbo);
    glBindVertexArray(tvg_vao);
    glBindBuffer(GL_ARRAY_BUFFER, tvg_vbo);
    glBufferData(GL_ARRAY_BUFFER, sizeof(quad), quad, GL_STATIC_DRAW);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 4*sizeof(float), (void*)0);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, 4*sizeof(float), (void*)(2*sizeof(float)));
    glEnableVertexAttribArray(1);
    glBindVertexArray(0);

    glGenTextures(1, &tvg_texture);
    glBindTexture(GL_TEXTURE_2D, tvg_texture);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
    glBindTexture(GL_TEXTURE_2D, 0);
    return true;
}

void tvg_init(int w, int h) {
    if (tvg_initialized || tvg_failed) return;

    auto res = tvg::Initializer::init(0);
    if (res != tvg::Result::Success) {
        fprintf(stderr, "[tvg] Initializer::init failed: %d\n", (int)res);
        tvg_failed = true;
        return;
    }

    tvg_canvas = tvg::SwCanvas::gen();
    if (!tvg_canvas) {
        fprintf(stderr, "[tvg] SwCanvas::gen() failed\n");
        tvg_failed = true;
        return;
    }

    tvg_w = w;
    tvg_h = h;
    tvg_buffer = new uint32_t[w * h];
    memset(tvg_buffer, 0, w * h * sizeof(uint32_t));

    res = tvg_canvas->target(tvg_buffer, w, w, h, tvg::ColorSpace::ABGR8888S);
    if (res != tvg::Result::Success) {
        fprintf(stderr, "[tvg] SwCanvas target failed: %d\n", (int)res);
        tvg_failed = true;
        return;
    }

    if (!init_gl_overlay(w, h)) {
        fprintf(stderr, "[tvg] GL overlay init failed\n");
        tvg_failed = true;
        return;
    }

    fprintf(stderr, "[tvg] initialized %dx%d\n", w, h);
    tvg_initialized = true;
}

void tvg_draw(UIState *s, int w, int h) {
    if (tvg_failed) return;

    if (!tvg_initialized || tvg_w != w || tvg_h != h) {
        if (tvg_initialized) tvg_destroy();
        tvg_init(w, h);
        if (tvg_failed) return;
    }

    memset(tvg_buffer, 0, tvg_w * tvg_h * sizeof(uint32_t));
    tvg_canvas->remove();

    float cx = w / 2.0f;

    auto bg = tvg::Shape::gen();
    bg->appendRect(cx - 110, 40, 220, 130, 25, 25);
    bg->fill(0, 0, 0, 160);
    tvg_canvas->add(bg);

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

    tvg_canvas->draw(true);
    tvg_canvas->sync();

    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glBindTexture(GL_TEXTURE_2D, tvg_texture);
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, tvg_w, tvg_h, GL_RGBA, GL_UNSIGNED_BYTE, tvg_buffer);
    glUseProgram(tvg_shader_program);
    glBindVertexArray(tvg_vao);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    glBindVertexArray(0);
    glUseProgram(0);
    glBindTexture(GL_TEXTURE_2D, 0);
    glDisable(GL_BLEND);
}

void tvg_destroy() {
    if (tvg_canvas) {
        delete tvg_canvas;
        tvg_canvas = nullptr;
    }
    if (tvg_buffer) {
        delete[] tvg_buffer;
        tvg_buffer = nullptr;
    }
    if (tvg_texture) { glDeleteTextures(1, &tvg_texture); tvg_texture = 0; }
    if (tvg_vao) { glDeleteVertexArrays(1, &tvg_vao); tvg_vao = 0; }
    if (tvg_vbo) { glDeleteBuffers(1, &tvg_vbo); tvg_vbo = 0; }
    if (tvg_shader_program) { glDeleteProgram(tvg_shader_program); tvg_shader_program = 0; }
    tvg::Initializer::term();
    tvg_initialized = false;
    tvg_failed = false;
}
