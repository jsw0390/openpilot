// CarrotPilot ThorVG HUD — GlCanvas(GPU) 우선 + SwCanvas(CPU) 자동 폴백
// 경로(path) + 차선(lane lines) + 도로 가장자리(road edges)

#include "selfdrive/ui/carrot_tvg.h"
#include "selfdrive/ui/qt/onroad/model.h"
#include <thorvg.h>

#ifdef __APPLE__
#include <OpenGL/gl3.h>
#else
#include <GLES3/gl3.h>
#endif

#include <cstring>
#include <cstdio>
#include <cmath>
#include <vector>

static tvg::Canvas *tvg_canvas = nullptr;
static int tvg_w = 0, tvg_h = 0;
static bool tvg_initialized = false;
static bool tvg_failed = false;
static bool tvg_use_gl = false;

// SwCanvas 폴백용
static uint32_t *tvg_buffer = nullptr;
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

static bool init_sw_overlay(int w, int h) {
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

static bool tvg_engine_initialized = false;

void tvg_init(int w, int h) {
    if (tvg_initialized || tvg_failed) return;

    if (!tvg_engine_initialized) {
        auto res = tvg::Initializer::init(0);
        if (res != tvg::Result::Success) {
            fprintf(stderr, "[tvg] Initializer::init failed: %d\n", (int)res);
            tvg_failed = true;
            return;
        }
        tvg_engine_initialized = true;
    }

    tvg_w = w;
    tvg_h = h;

    tvg::Result res;

    // GlCanvas 시도 (GLES 3.0+)
    const char *gl_ver = (const char *)glGetString(GL_VERSION);
    bool gles_ok = gl_ver && strstr(gl_ver, "OpenGL ES 3");
    fprintf(stderr, "[tvg] GL: %s\n", gl_ver ? gl_ver : "null");

    if (gles_ok) {
        auto gl = tvg::GlCanvas::gen();
        if (!gl) {
            fprintf(stderr, "[tvg] GlCanvas::gen() returned null\n");
        } else {
            res = gl->target(nullptr, nullptr, nullptr, 0, w, h, tvg::ColorSpace::ABGR8888S);
            if (res == tvg::Result::Success) {
                tvg_canvas = gl;
                tvg_use_gl = true;
                fprintf(stderr, "[tvg] GlCanvas %dx%d (GPU)\n", w, h);
                tvg_initialized = true;
                return;
            }
            fprintf(stderr, "[tvg] GlCanvas::target() failed: %d\n", (int)res);
            delete gl;
        }
        fprintf(stderr, "[tvg] GlCanvas failed, using SwCanvas\n");
    }

    // SwCanvas 폴백
    auto sw = tvg::SwCanvas::gen();
    if (!sw) {
        fprintf(stderr, "[tvg] SwCanvas::gen() failed\n");
        tvg_failed = true;
        return;
    }

    tvg_buffer = new uint32_t[w * h];
    memset(tvg_buffer, 0, w * h * sizeof(uint32_t));
    res = sw->target(tvg_buffer, w, w, h, tvg::ColorSpace::ABGR8888S);
    if (res != tvg::Result::Success) {
        fprintf(stderr, "[tvg] SwCanvas target failed: %d\n", (int)res);
        delete sw;
        delete[] tvg_buffer;
        tvg_buffer = nullptr;
        tvg_failed = true;
        return;
    }

    if (!init_sw_overlay(w, h)) {
        fprintf(stderr, "[tvg] SW overlay init failed\n");
        delete sw;
        delete[] tvg_buffer;
        tvg_buffer = nullptr;
        tvg_failed = true;
        return;
    }

    tvg_canvas = sw;
    tvg_use_gl = false;
    fprintf(stderr, "[tvg] SwCanvas %dx%d (CPU)\n", w, h);
    tvg_initialized = true;
}

// QPolygonF → ThorVG Shape 변환 (좌표 검증 포함)
static tvg::Shape* polygon_to_shape(const QPolygonF &poly, uint8_t r, uint8_t g, uint8_t b, uint8_t a) {
    if (poly.size() < 3) return nullptr;

    // 유효한 좌표만 수집
    std::vector<std::pair<float,float>> pts;
    for (int i = 0; i < poly.size(); i++) {
        float x = poly[i].x(), y = poly[i].y();
        if (std::isfinite(x) && std::isfinite(y) &&
            x > -10000 && x < 10000 && y > -10000 && y < 10000) {
            pts.push_back({x, y});
        }
    }
    if (pts.size() < 3) return nullptr;

    auto shape = tvg::Shape::gen();
    shape->moveTo(pts[0].first, pts[0].second);
    for (size_t i = 1; i < pts.size(); i++) {
        shape->lineTo(pts[i].first, pts[i].second);
    }
    shape->close();
    shape->fill(r, g, b, a);
    return shape;
}

// cereal에서 직접 경로 폴리곤 계산 (carrot.cc와 동일 방식)
static void calc_path_polygon(ModelRenderer *model, const UIState *s, QPolygonF &out) {
    out.clear();
    if (!model || !s || !s->sm) return;
    auto &sm = *(s->sm);

    if (sm.rcv_frame("modelV2") < s->scene.started_frame ||
        sm.rcv_frame("liveCalibration") < s->scene.started_frame) return;

    // clip_region 설정 (model.draw()가 해주던 것)
    model->clip_region = QRectF(0, 0, s->fb_w, s->fb_h).adjusted(-500, -500, 500, 500);

    const auto &modelV2 = sm["modelV2"].getModelV2();
    const auto &pos = modelV2.getPosition();
    const auto pos_x = pos.getX(), pos_y = pos.getY(), pos_z = pos.getZ();

    float max_dist = std::clamp(*(pos_x.end() - 1), 10.0f, 100.0f);

    // lead 차량 고려
    if (sm.alive("radarState")) {
        const auto &lead = sm["radarState"].getRadarState().getLeadOne();
        if (lead.getStatus()) {
            float lead_d = lead.getDRel() * 2.0f;
            max_dist = std::clamp(lead_d - fminf(lead_d * 0.35f, 10.f), 0.0f, max_dist);
        }
    }

    int max_idx = 0;
    for (int i = 1; i < (int)pos_x.size() && pos_x[i] <= max_dist; ++i) max_idx = i;

    float path_offset_z = sm["liveCalibration"].getLiveCalibration().getHeight()[0];

    QPolygonF left_pts, right_pts;
    for (int i = 0; i <= max_idx; i++) {
        if (pos_x[i] < 0) continue;
        QPointF left, right;
        bool l = model->mapToScreen(pos_x[i], pos_y[i] - 0.9f, pos_z[i] + path_offset_z, &left);
        bool r = model->mapToScreen(pos_x[i], pos_y[i] + 0.9f, pos_z[i] + path_offset_z, &right);
        if (l && r) {
            if (left_pts.size() && left.y() > left_pts.back().y()) continue;
            left_pts.push_back(left);
            right_pts.push_front(right);
        }
    }
    out = left_pts + right_pts;
}

// cereal에서 직접 차선 폴리곤 계산
static void calc_lane_polygons(ModelRenderer *model, const UIState *s,
                                QPolygonF lane_verts[4], float lane_probs[4],
                                QPolygonF edge_verts[2], float edge_stds[2]) {
    if (!model || !s || !s->sm) return;
    auto &sm = *(s->sm);

    if (sm.rcv_frame("modelV2") < s->scene.started_frame ||
        sm.rcv_frame("liveCalibration") < s->scene.started_frame) return;

    const auto &modelV2 = sm["modelV2"].getModelV2();
    const auto &pos = modelV2.getPosition();
    float max_dist = std::clamp(*(pos.getX().end() - 1), 10.0f, 100.0f);

    // 차선
    const auto &lanes = modelV2.getLaneLines();
    const auto &probs = modelV2.getLaneLineProbs();
    int max_idx = 0;
    for (int i = 1; i < (int)lanes[0].getX().size() && lanes[0].getX()[i] <= max_dist; ++i) max_idx = i;

    for (int i = 0; i < 4; i++) {
        lane_probs[i] = probs[i];
        const auto lx = lanes[i].getX(), ly = lanes[i].getY(), lz = lanes[i].getZ();
        QPolygonF left_pts, right_pts;
        float y_off = 0.025f * lane_probs[i];
        for (int j = 0; j <= max_idx; j++) {
            if (lx[j] < 0) continue;
            QPointF left, right;
            bool l = model->mapToScreen(lx[j], ly[j] - y_off, lz[j], &left);
            bool r = model->mapToScreen(lx[j], ly[j] + y_off, lz[j], &right);
            if (l && r) {
                left_pts.push_back(left);
                right_pts.push_front(right);
            }
        }
        lane_verts[i] = left_pts + right_pts;
    }

    // 도로 가장자리
    const auto &edges = modelV2.getRoadEdges();
    const auto &estds = modelV2.getRoadEdgeStds();
    for (int i = 0; i < 2; i++) {
        edge_stds[i] = estds[i];
        const auto ex = edges[i].getX(), ey = edges[i].getY(), ez = edges[i].getZ();
        QPolygonF left_pts, right_pts;
        for (int j = 0; j <= max_idx; j++) {
            if (ex[j] < 0) continue;
            QPointF left, right;
            bool l = model->mapToScreen(ex[j], ey[j] - 0.025f, ez[j], &left);
            bool r = model->mapToScreen(ex[j], ey[j] + 0.025f, ez[j], &right);
            if (l && r) {
                left_pts.push_back(left);
                right_pts.push_front(right);
            }
        }
        edge_verts[i] = left_pts + right_pts;
    }
}

static void draw_path(ModelRenderer *model, UIState *s, int w, int h) {
    QPolygonF path;
    calc_path_polygon(model, s, path);
    if (path.size() < 3) return;

    auto shape = polygon_to_shape(path, 23, 134, 68, 100);
    if (shape) tvg_canvas->add(shape);
}

static void draw_lanes(ModelRenderer *model, UIState *s) {
    QPolygonF lane_verts[4], edge_verts[2];
    float lane_probs[4] = {}, edge_stds[2] = {};
    calc_lane_polygons(model, s, lane_verts, lane_probs, edge_verts, edge_stds);

    for (int i = 0; i < 4; i++) {
        if (lane_verts[i].size() < 3) continue;
        uint8_t alpha = (uint8_t)(std::clamp(lane_probs[i], 0.0f, 0.7f) * 255);
        auto lane = polygon_to_shape(lane_verts[i], 255, 255, 255, alpha);
        if (lane) tvg_canvas->add(lane);
    }

    for (int i = 0; i < 2; i++) {
        if (edge_verts[i].size() < 3) continue;
        uint8_t alpha = (uint8_t)(std::clamp(1.0f - edge_stds[i], 0.0f, 1.0f) * 255);
        auto edge = polygon_to_shape(edge_verts[i], 255, 0, 0, alpha);
        if (edge) tvg_canvas->add(edge);
    }
}

static void draw_hud(UIState *s, int w, int h) {
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
}

void tvg_draw(UIState *s, int w, int h, ModelRenderer *model) {
    if (tvg_failed) return;

    if (!tvg_initialized || tvg_w != w || tvg_h != h) {
        if (tvg_initialized) tvg_destroy();
        tvg_init(w, h);
        if (tvg_failed) return;
    }

    if (!tvg_use_gl && tvg_buffer) {
        memset(tvg_buffer, 0, tvg_w * tvg_h * sizeof(uint32_t));
    }

    tvg_canvas->remove();

    // 경로 + 차선
    draw_path(model, s, w, h);
    draw_lanes(model, s);

    // HUD 요소
    draw_hud(s, w, h);

    if (tvg_use_gl) {
        glEnable(GL_BLEND);
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
        tvg_canvas->draw(true);
        tvg_canvas->sync();
        glDisable(GL_BLEND);
    } else {
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
    // Initializer::term() 제거 — 리사이즈 시 재초기화 문제 방지
    tvg_initialized = false;
    tvg_failed = false;
}
