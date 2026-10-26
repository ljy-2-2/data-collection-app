VARYING vec3 robotNormal;
void MAIN() {
    vec3 n = normalize(robotNormal);
    if (!gl_FrontFacing) n = -n;
    // Front-facing key plus reverse fill; keep unlit surfaces readable.
    float light = .50 + .35 * max(dot(n,normalize(vec3(.6,-.4,1.))),0.) + .15 * max(dot(n,normalize(vec3(-.5,.6,.5))),0.);
    FRAGCOLOR = vec4(robotColor * light, 1.0);
}
