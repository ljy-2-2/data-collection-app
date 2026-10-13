VARYING vec3 robotNormal;
void MAIN() {
    robotNormal = mat3(MODEL_MATRIX) * NORMAL;
    POSITION = MODELVIEWPROJECTION_MATRIX * vec4(VERTEX, 1.0);
}
