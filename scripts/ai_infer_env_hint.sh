#!/bin/sh
# Rychlá nápověda k proměnným inference (spouštět na hostu / v ai_core).
printf '%s\n' \
  "RPY_INFER_BACKEND=stub|onnx|hailo  (prázdné = legacy USE_HAILO)" \
  "RPY_ONNX_MODEL_PATH=/path/to/yolov8n.onnx" \
  "RPY_HAILO_HEF_PATH=/path/to/model.hef" \
  "USE_HAILO=0|1" \
  "Diagnostika UI: tlačítko Diagnostika stacku → ai_infer_stack"
