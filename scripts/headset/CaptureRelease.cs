using System.Linq;
using UnityEditor;
using UnityEditor.Build.Reporting;
public static class CaptureRelease {
 public static void Build() {
  PlayerSettings.bundleVersion = "1.1.7-capture.1";
  PlayerSettings.Android.bundleVersionCode = 2;
  var result = BuildPipeline.BuildPlayer(new BuildPlayerOptions {
   scenes = EditorBuildSettings.scenes.Where(s => s.enabled).Select(s => s.path).ToArray(),
   locationPathName = "../galaxis-vr-capture.apk", target = BuildTarget.Android, options = BuildOptions.None
  });
  EditorApplication.Exit(result.summary.result == BuildResult.Succeeded ? 0 : 1);
 }
}
