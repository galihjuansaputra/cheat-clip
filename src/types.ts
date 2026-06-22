export interface HeatmapPoint {
  start_time: number;
  end_time: number;
  value: number;
}

export interface ViralClip {
  title: string;
  start_time: number;
  end_time: number;
  virality_score: number;
  key_quotes: string[];
  transcript: string;
}

export interface AnalyzeResponse {
  video_id: string;
  title: string;
  duration: number;
  heatmap: HeatmapPoint[];
  summary: string;
  clips: ViralClip[];
}
