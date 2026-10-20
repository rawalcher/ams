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
in
{
  packages = [
    convert-cpu
    convert-gpu
    pkgs.ffmpeg
    pkgs.libva
    pkgs.libva-utils
    pkgs.vulkan-tools
    pkgs.vulkan-loader
  ];
}
