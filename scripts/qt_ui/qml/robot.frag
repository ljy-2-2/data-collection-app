VARYING vec3 robotNormal;
void MAIN() {
    vec3 n = normalize(robotNormal);
    if (!gl_FrontFacing) n = -n;
    float light = .32 + .58 * max(dot(n,normalize(vec3(-.6,-.4,1.))),0.) + .1 * max(dot(n,normalize(vec3(.5,.6,.2))),0.);
    FRAGCOLOR = vec4(robotColor * light, 1.0);
}
