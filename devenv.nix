{
  pkgs,
  lib,
  ...
}:

let
  convert-cpu = pkgs.writeShellApplication {
    name = "t1-cpu";
    runtimeInputs = [ pkgs.ffmpeg ];
    text = ''
      exec ${./task1/convert_cpu.sh} original_video/bbb_sunflower_1080p_60fps_normal.mp4 task1/cpu_out
    '';
  };

  convert-gpu = pkgs.writeShellApplication {
    name = "t1-gpu";
    runtimeInputs = [ pkgs.ffmpeg pkgs.libva ];
    text = ''
      exec ${./task1/convert_amd_gpu.sh} original_video/bbb_sunflower_1080p_60fps_normal.mp4 task1/gpu_out
    '';
  };

  zip-tasks = pkgs.writeShellApplication {
    name = "t1-zip";
    text = ''
      set -e
      for dir in task1/cpu_out task1/gpu_out; do
        if [ -d "$dir" ]; then
          tar czf "$dir.tar.gz" -C "$dir" .
          echo "Created $dir.tar.gz"
        fi
      done
    '';
  };
in
{
  packages = [
    convert-cpu
    convert-gpu
    zip-tasks
    pkgs.ffmpeg
    pkgs.libva
    pkgs.libva-utils
    pkgs.vulkan-tools
    pkgs.vulkan-loader
  ];
}
