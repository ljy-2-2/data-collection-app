pragma Singleton
import QtQuick
QtObject {
    readonly property bool dark: backend.state.theme === "dark" || (backend.state.theme === "system" && systemDark)
    readonly property color bg: dark ? "#111111" : "#f5f5f5"
    readonly property color surface: dark ? "#1a1a1a" : "#ffffff"
    readonly property color muted: dark ? "#151515" : "#f8f8f8"
    readonly property color raised: dark ? "#222222" : "#fafafa"
    readonly property color ink: dark ? "#f5f5f5" : "#333333"
    readonly property color inkMuted: dark ? "#b2b2b2" : "#686868"
    readonly property color inkSoft: dark ? "#808080" : "#989898"
    readonly property color line: dark ? "#2f2f2f" : "#e5e5e5"
    readonly property color lineStrong: dark ? "#474747" : "#cdcdcd"
    readonly property color accent: "#f4c520"
    readonly property color accentStrong: dark ? "#ffd84f" : "#b98b00"
    readonly property color accentSoft: dark ? "#362f13" : "#fff7d1"
    readonly property color green: dark ? "#7fd99d" : "#16845b"
    readonly property color greenSoft: dark ? "#173522" : "#e8f7f0"
    readonly property color red: dark ? "#ff9b91" : "#c2362b"
    readonly property color redSoft: dark ? "#421d1b" : "#fff0ee"
    readonly property color amber: dark ? "#e2b460" : "#a56708"
    readonly property color amberSoft: dark ? "#3d2e13" : "#fff5dd"
    readonly property color blue: dark ? "#8fb1ff" : "#4f6df5"
    readonly property color blueSoft: dark ? "#172949" : "#edf1ff"
    readonly property color stage: dark ? "#171c24" : "#edf1f5"
    readonly property string font: "Ubuntu"
    function icon(name, tone) {return "../icons/"+name+(tone ? "-"+tone : "")+(dark ? "-dark" : "")+".svg"}
}
