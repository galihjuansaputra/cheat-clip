import { useState, useEffect, useRef, useMemo } from 'react';
import { HeatmapTimeline } from './components/HeatmapTimeline';
import type { AnalyzeResponse, ViralClip } from './types';

// Declare YT global variables for TypeScript
declare global {
  interface Window {
    YT: any;
    onYouTubeIframeAPIReady: (() => void) | undefined;
  }
}

export default function App() {
  const [url, setUrl] = useState('');
  const [durationPref, setDurationPref] = useState<'15s' | '30s' | '60s'>('30s');
  const [apiKey, setApiKey] = useState(() => localStorage.getItem('cheat_clip_gemini_api_key') || '');
  const [showApiKey, setShowApiKey] = useState(false);

  // AI model selection and custom focus prompt states
  const [selectedModel, setSelectedModel] = useState<string>(() => {
    const saved = localStorage.getItem('cheat_clip_selected_model');
    // Auto-migrate outdated or 404 models like gemini-1.5-* to gemini-2.5-flash
    if (saved && (saved.includes('1.5') || saved.includes('1.0'))) {
      localStorage.setItem('cheat_clip_selected_model', 'gemini-2.5-flash');
      return 'gemini-2.5-flash';
    }
    return saved || 'gemini-2.5-flash';
  });
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [loadingModels, setLoadingModels] = useState(false);
  const [customPrompt, setCustomPrompt] = useState<string>('');
  const [targetClipCount, setTargetClipCount] = useState<number>(() => {
    const val = localStorage.getItem('cheat_clip_target_clip_count');
    return val ? Number(val) : 10;
  });

  // Custom range selection states
  const [rangeType, setRangeType] = useState<'entire' | 'custom'>('entire');
  const [customRangeStart, setCustomRangeStart] = useState<string>('');
  const [customRangeEnd, setCustomRangeEnd] = useState<string>('');

  // Manual subtitles states
  const [subtitlesSource, setSubtitlesSource] = useState<'youtube' | 'manual'>('youtube');
  const [manualSubtitlesContent, setManualSubtitlesContent] = useState<string>('');
  const [manualSubtitlesFileName, setManualSubtitlesFileName] = useState<string>('');

  const parseTimeToSeconds = (val: string): number | null => {
    const clean = val.trim();
    if (!clean) return null;

    // Check if it's just raw number of seconds
    if (/^\d+(\.\d+)?$/.test(clean)) {
      return parseFloat(clean);
    }

    const parts = clean.split(':').map(Number);
    if (parts.some(isNaN)) return null;

    if (parts.length === 2) {
      // MM:SS
      return parts[0] * 60 + parts[1];
    } else if (parts.length === 3) {
      // HH:MM:SS
      return parts[0] * 3600 + parts[1] * 60 + parts[2];
    }
    return null;
  };

  // Loading & process states
  const [loading, setLoading] = useState(false);
  const [currentStep, setCurrentStep] = useState(1);
  const [error, setError] = useState<string | null>(null);

  // Real-time progress and cognitive stage tracking
  const [stepProgress, setStepProgress] = useState<Record<number, number>>({ 1: 0, 2: 0, 3: 0, 4: 0 });
  const [overallProgress, setOverallProgress] = useState<number>(0);
  const [aiStage, setAiStage] = useState<string>('');
  const [aiDetail, setAiDetail] = useState<string>('');
  const [activeProcessingModel, setActiveProcessingModel] = useState<string>('');
  const [loadingElapsedTime, setLoadingElapsedTime] = useState<number>(0);

  // Active timer during loading so the user always sees live activity
  useEffect(() => {
    let interval: number | null = null;
    if (loading) {
      setLoadingElapsedTime(0);
      interval = window.setInterval(() => {
        setLoadingElapsedTime(prev => prev + 1);
      }, 1000);
    } else {
      setLoadingElapsedTime(0);
    }
    return () => {
      if (interval !== null) clearInterval(interval);
    };
  }, [loading]);

  // Results
  const [result, setResult] = useState<AnalyzeResponse | null>(null);
  const [activeClip, setActiveClip] = useState<ViralClip | null>(null);
  const [expandedClipIndex, setExpandedClipIndex] = useState<number | null>(null);

  const leftPanelRef = useRef<HTMLDivElement>(null);
  const [leftPanelHeight, setLeftPanelHeight] = useState<number | null>(null);

  useEffect(() => {
    if (!result) return;
    const updateHeight = () => {
      if (leftPanelRef.current) {
        setLeftPanelHeight(leftPanelRef.current.clientHeight);
      }
    };

    updateHeight();
    window.addEventListener('resize', updateHeight);

    const observer = new ResizeObserver(updateHeight);
    if (leftPanelRef.current) {
      observer.observe(leftPanelRef.current);
    }

    return () => {
      window.removeEventListener('resize', updateHeight);
      observer.disconnect();
    };
  }, [result, activeClip, expandedClipIndex]);

  // Search & Filtering States
  const [searchQuery, setSearchQuery] = useState('');
  const [viralityFilter, setViralityFilter] = useState<'all' | 'high' | 'medium' | 'marked'>('all');
  const [sortBy, setSortBy] = useState<'virality' | 'time' | 'duration' | 'marked'>('virality');

  // Assistance feature: Checklist for marked clips
  const [markedClips, setMarkedClips] = useState<Record<string, boolean>>({});
  const [loadingDetails, setLoadingDetails] = useState('');

  // History feature: previously analyzed videos from localStorage
  interface HistoryEntry {
    video_id: string;
    title: string;
    duration_pref: string;
    clip_count: number;
    analyzed_at: string;
    thumbnail: string;
    url: string;
    range_suffix?: string;
  }
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [showHistory, setShowHistory] = useState(false);

  // Audio/video playback state tracking
  const [currentTime, setCurrentTime] = useState(0);
  const playerRef = useRef<any>(null);
  const trackingInterval = useRef<number | null>(null);
  const clipEndIntervalRef = useRef<number | null>(null);
  const loadingSectionRef = useRef<HTMLElement | null>(null);
  const [toastMessage, setToastMessage] = useState<string | null>(null);

  // Automatically scroll down to the loading progress section when analysis starts
  useEffect(() => {
    if (loading) {
      const scrollTimer = setTimeout(() => {
        if (loadingSectionRef.current) {
          loadingSectionRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } else {
          const el = document.getElementById('loading-progress-section');
          el?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      }, 100);
      return () => clearTimeout(scrollTimer);
    }
  }, [loading]);

  // Initialize YouTube IFrame API
  useEffect(() => {
    // Check if script is already injected
    const existingScript = document.getElementById('youtube-iframe-api-script');
    if (!existingScript) {
      const tag = document.createElement('script');
      tag.id = 'youtube-iframe-api-script';
      tag.src = 'https://www.youtube.com/iframe_api';
      const firstScriptTag = document.getElementsByTagName('script')[0];
      firstScriptTag.parentNode?.insertBefore(tag, firstScriptTag);
    }

    // Set global callback
    window.onYouTubeIframeAPIReady = () => {
      // Re-trigger player init if a result is already loaded
      if (result) {
        initPlayer(result.video_id);
      }
    };

    return () => {
      stopTracking();
      if (clipEndIntervalRef.current !== null) {
        clearInterval(clipEndIntervalRef.current);
      }
    };
  }, [result]);

  // Fetch available AI models when API key is detected/entered
  useEffect(() => {
    const fetchModels = async () => {
      const cleanKey = apiKey.trim();
      if (!cleanKey || cleanKey.length < 20 || cleanKey.toLowerCase() === 'mock') {
        setAvailableModels([]);
        return;
      }
      setLoadingModels(true);
      try {
        const res = await fetch(`/api/models?api_key=${encodeURIComponent(cleanKey)}`);
        if (res.ok) {
          const data = await res.json();
          if (data.models && data.models.length > 0) {
            setAvailableModels(data.models);
            if (!data.models.includes(selectedModel) || selectedModel.includes('1.5') || selectedModel.includes('1.0')) {
              const fallback = data.models.find((m: string) => m.includes('flash')) || data.models[0] || 'gemini-2.5-flash';
              setSelectedModel(fallback);
              localStorage.setItem('cheat_clip_selected_model', fallback);
            }
          }
        }
      } catch (err) {
        console.error('Failed to retrieve available models:', err);
      } finally {
        setLoadingModels(false);
      }
    };

    const delayDebounce = setTimeout(() => {
      fetchModels();
    }, 600);

    return () => clearTimeout(delayDebounce);
  }, [apiKey]);

  // Sync marked clips with local storage based on active video ID
  useEffect(() => {
    if (result?.video_id) {
      const saved = localStorage.getItem(`marked_clips_${result.video_id}`);
      if (saved) {
        try {
          setMarkedClips(JSON.parse(saved));
        } catch (_) {
          setMarkedClips({});
        }
      } else {
        setMarkedClips({});
      }
    } else {
      setMarkedClips({});
    }
  }, [result]);

  // Clear expanded clip index when filters or sorting change
  useEffect(() => {
    setExpandedClipIndex(null);
  }, [sortBy, viralityFilter, searchQuery]);

  const toggleMarkedClip = (clipId: string) => {
    if (!result?.video_id) return;
    const updated = {
      ...markedClips,
      [clipId]: !markedClips[clipId]
    };
    setMarkedClips(updated);
    localStorage.setItem(`marked_clips_${result.video_id}`, JSON.stringify(updated));
  };

  // Scan localStorage and build the history list from cache keys
  const refreshHistory = () => {
    const entries: HistoryEntry[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key && key.startsWith('cheat_clip_cache_')) {
        try {
          const raw = localStorage.getItem(key);
          if (!raw) continue;
          const data: AnalyzeResponse = JSON.parse(raw);

          if (!data || !data.video_id) continue;

          const video_id = data.video_id;
          const rest = key.substring('cheat_clip_cache_'.length);
          const suffix = rest.substring(video_id.length + 1); // skip video_id and trailing '_'
          const duration_pref = suffix.split('_')[0];
          const range_suffix = suffix.substring(duration_pref.length);

          // Try reading cached timestamp stored separately
          const tsKey = `cheat_clip_ts_${video_id}_${duration_pref}${range_suffix}`;
          const analyzed_at = localStorage.getItem(tsKey) || new Date().toISOString();
          entries.push({
            video_id,
            title: data.title,
            duration_pref,
            clip_count: data.clips?.length || 0,
            analyzed_at,
            thumbnail: `https://img.youtube.com/vi/${video_id}/mqdefault.jpg`,
            url: `https://www.youtube.com/watch?v=${video_id}`,
            range_suffix
          });
        } catch (_) {
          // Skip malformed entries
        }
      }
    }
    // Sort by most recent first
    entries.sort((a, b) => new Date(b.analyzed_at).getTime() - new Date(a.analyzed_at).getTime());
    setHistory(entries);
  };

  // Load history on mount
  useEffect(() => {
    refreshHistory();
  }, []);

  const loadFromHistory = (entry: HistoryEntry) => {
    const rangeSuffix = entry.range_suffix || '';
    const cacheKey = `cheat_clip_cache_${entry.video_id}_${entry.duration_pref}${rangeSuffix}`;
    const raw = localStorage.getItem(cacheKey);
    if (!raw) return;
    try {
      const data: AnalyzeResponse = JSON.parse(raw);
      setUrl(`https://www.youtube.com/watch?v=${entry.video_id}`);
      setDurationPref(entry.duration_pref as '15s' | '30s' | '60s');

      // Restore subtitle source state
      if (entry.range_suffix?.includes('_manual')) {
        setSubtitlesSource('manual');
      } else {
        setSubtitlesSource('youtube');
      }

      // Restore range inputs if they were custom
      if (entry.range_suffix) {
        const cleanRangeSuffix = entry.range_suffix.replace('_manual', '');
        const rangeMatch = cleanRangeSuffix.match(/_range_([^_]+)_([^_]+)/);
        if (rangeMatch) {
          setRangeType('custom');
          const startVal = rangeMatch[1];
          const endVal = rangeMatch[2];

          setCustomRangeStart(startVal !== '0' ? formatSeconds(Number(startVal)) : '');
          setCustomRangeEnd(endVal !== 'end' ? formatSeconds(Number(endVal)) : '');
        } else {
          setRangeType('entire');
          setCustomRangeStart('');
          setCustomRangeEnd('');
        }
      } else {
        setRangeType('entire');
        setCustomRangeStart('');
        setCustomRangeEnd('');
      }

      // Destroy any existing player immediately before state resets
      destroyPlayer();
      setLoading(true);
      setError(null);
      setResult(null);
      setActiveClip(null);
      setCurrentStep(1);
      setStepProgress({ 1: 100, 2: 0, 3: 0, 4: 0 });
      setOverallProgress(25);
      setLoadingDetails('Loading from history...');
      setTimeout(() => {
        setCurrentStep(4);
        setStepProgress({ 1: 100, 2: 100, 3: 100, 4: 85 });
        setOverallProgress(90);
        setAiStage('Restoring Viral Hotspots');
        setAiDetail('Reconstructing engagement timestamps from saved analysis...');
        setLoadingDetails('Restoring viral hotspots from saved analysis...');
        setTimeout(() => {
          setStepProgress({ 1: 100, 2: 100, 3: 100, 4: 100 });
          setOverallProgress(100);
          setResult(data);
          setLoading(false);
          setShowHistory(false);
          if (data.clips?.length > 0) setActiveClip(data.clips[0]);
          // Force recreate the player since we destroyed it
          setTimeout(() => initPlayer(data.video_id, true), 150);
        }, 500);
      }, 600);
    } catch (_) {
      setToastMessage('Failed to load this history entry.');
      setTimeout(() => setToastMessage(null), 3000);
    }
  };

  const deleteHistoryEntry = (entry: HistoryEntry, e: React.MouseEvent) => {
    e.stopPropagation();
    const rangeSuffix = entry.range_suffix || '';
    const cacheKey = `cheat_clip_cache_${entry.video_id}_${entry.duration_pref}${rangeSuffix}`;
    const tsKey = `cheat_clip_ts_${entry.video_id}_${entry.duration_pref}${rangeSuffix}`;
    localStorage.removeItem(cacheKey);
    localStorage.removeItem(tsKey);
    refreshHistory();
    setToastMessage(`Removed "${entry.title}" from history.`);
    setTimeout(() => setToastMessage(null), 3000);
  };

  const clearAllHistory = () => {
    const toRemove: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key && (key.startsWith('cheat_clip_cache_') || key.startsWith('cheat_clip_ts_'))) {
        toRemove.push(key);
      }
    }
    toRemove.forEach(k => localStorage.removeItem(k));
    setHistory([]);
    setToastMessage('All history cleared.');
    setTimeout(() => setToastMessage(null), 3000);
  };

  const formatRelativeTime = (iso: string) => {
    const diff = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.floor(hrs / 24);
    return `${days}d ago`;
  };


  const destroyPlayer = () => {
    stopTracking();
    if (playerRef.current) {
      try {
        if (typeof playerRef.current.destroy === 'function') {
          playerRef.current.destroy();
        }
      } catch (e) {
        console.warn('Error destroying player:', e);
      }
      playerRef.current = null;
    }
    // Re-create the div placeholder (destroy() removes the iframe but leaves the div empty)
    const container = document.getElementById('youtube-player-container');
    if (container) {
      container.innerHTML = '<div id="youtube-player"></div>';
    }
  };

  const initPlayer = (videoId: string, forceRecreate = false) => {
    // If player already exists and we're not forcing recreate, try to load new video
    if (!forceRecreate && playerRef.current && typeof playerRef.current.loadVideoById === 'function') {
      try {
        playerRef.current.loadVideoById(videoId);
        return;
      } catch (e) {
        console.error('Failed to load video on existing player, will recreate...', e);
      }
    }

    // Ensure target container is rendered in the DOM before instantiating the player.
    // If React hasn't completed mounting the dashboard yet, wait and retry.
    const container = document.getElementById('youtube-player-container');
    if (!container) {
      setTimeout(() => initPlayer(videoId, forceRecreate), 100);
      return;
    }

    // Destroy any stale player first
    if (playerRef.current) {
      destroyPlayer();
    }

    // Create player if YT API is loaded
    if (window.YT && window.YT.Player) {
      if (!document.getElementById('youtube-player')) {
        container.innerHTML = '<div id="youtube-player"></div>';
      }
      try {
        playerRef.current = new window.YT.Player('youtube-player', {
          videoId: videoId,
          playerVars: {
            autoplay: 0,
            modestbranding: 1,
            rel: 0,
            controls: 1,
            fs: 1,
          },
          events: {
            onReady: () => {
              console.log('YouTube Player Ready');
            },
            onStateChange: (event: any) => {
              // YT.PlayerState.PLAYING = 1
              if (event.data === 1) {
                startTracking();
              } else {
                stopTracking();
                // Update currentTime on pause/stop to sync cursor
                if (playerRef.current && playerRef.current.getCurrentTime) {
                  setCurrentTime(playerRef.current.getCurrentTime());
                }
              }
            },
          },
        });
      } catch (err) {
        console.error('Error instantiating YouTube Player:', err);
        // Fallback retry in case of transient iframe injection issues
        setTimeout(() => initPlayer(videoId, forceRecreate), 300);
      }
    } else {
      // Try again in 200ms if global window.YT is not ready yet
      setTimeout(() => initPlayer(videoId, forceRecreate), 200);
    }
  };

  const startTracking = () => {
    stopTracking();
    trackingInterval.current = window.setInterval(() => {
      if (playerRef.current && typeof playerRef.current.getCurrentTime === 'function') {
        setCurrentTime(playerRef.current.getCurrentTime());
      }
    }, 200);
  };

  const stopTracking = () => {
    if (trackingInterval.current !== null) {
      clearInterval(trackingInterval.current);
      trackingInterval.current = null;
    }
  };

  const handleSeek = (seconds: number) => {
    if (playerRef.current && typeof playerRef.current.seekTo === 'function') {
      playerRef.current.seekTo(seconds, true);
      setCurrentTime(seconds);
      // If paused, play it
      if (playerRef.current.getPlayerState() !== 1) {
        playerRef.current.playVideo();
      }
    }
  };

  const playClip = (clip: ViralClip) => {
    setActiveClip(clip);
    handleSeek(clip.start_time);

    if (clipEndIntervalRef.current !== null) {
      clearInterval(clipEndIntervalRef.current);
    }

    // Automatically stop video at end time (optional user experience feature)
    // We can monitor playback and pause if it goes past end_time
    const intervalId = window.setInterval(() => {
      if (playerRef.current && typeof playerRef.current.getCurrentTime === 'function') {
        const curr = playerRef.current.getCurrentTime();
        if (curr >= clip.end_time) {
          playerRef.current.pauseVideo();
          clearInterval(intervalId);
          if (clipEndIntervalRef.current === intervalId) {
            clipEndIntervalRef.current = null;
          }
        }
      } else {
        clearInterval(intervalId);
        if (clipEndIntervalRef.current === intervalId) {
          clipEndIntervalRef.current = null;
        }
      }
    }, 300);

    clipEndIntervalRef.current = intervalId;
  };

  const extractVideoId = (urlStr: string): string | null => {
    const patterns = [
      /(?:v=|\/v\/|embed\/|shorts\/|youtu\.be\/|\/embed\/|\/watch\?v=|\/watch\?.+&v=)([^#\&\?]{11})/,
      /^(?:https?:\/\/)?(?:www\.)?(?:youtube\.com|youtu\.be)\/(?:watch\?v=)?([^#\&\?]{11})/
    ];
    for (const pattern of patterns) {
      const match = urlStr.match(pattern);
      if (match) {
        return match[1];
      }
    }
    const trimmed = urlStr.trim();
    if (trimmed.length === 11) {
      return trimmed;
    }
    return null;
  };

  const handleAnalyze = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!url.trim()) return;

    // Require an API key before making any request
    if (!apiKey.trim()) {
      setError('A Gemini API Key is required to analyze videos. Get a free key at aistudio.google.com and paste it in the field below.');
      return;
    }

    let rangeStartSecs: number | undefined = undefined;
    let rangeEndSecs: number | undefined = undefined;

    if (rangeType === 'custom') {
      const parsedStart = parseTimeToSeconds(customRangeStart);
      const parsedEnd = parseTimeToSeconds(customRangeEnd);

      if (customRangeStart.trim() && parsedStart === null) {
        setError('Invalid start time format. Please use MM:SS (e.g. 29:00), HH:MM:SS, or raw seconds.');
        return;
      }
      if (customRangeEnd.trim() && parsedEnd === null) {
        setError('Invalid end time format. Please use MM:SS (e.g. 31:15), HH:MM:SS, or raw seconds.');
        return;
      }

      if (parsedStart !== null) rangeStartSecs = parsedStart;
      if (parsedEnd !== null) rangeEndSecs = parsedEnd;

      if (rangeStartSecs !== undefined && rangeEndSecs !== undefined && rangeStartSecs >= rangeEndSecs) {
        setError('Start time must be less than end time.');
        return;
      }
    }

    if (subtitlesSource === 'manual' && !manualSubtitlesContent.trim()) {
      setError('Please choose or drag-and-drop a custom subtitle file (.srt or .txt).');
      return;
    }

    // Check localStorage cache first to avoid redundant API/Gemini processing
    const videoId = extractVideoId(url);
    const rangeSuffix = (rangeStartSecs !== undefined || rangeEndSecs !== undefined)
      ? `_range_${rangeStartSecs ?? 0}_${rangeEndSecs ?? 'end'}`
      : '';
    const manualSuffix = subtitlesSource === 'manual' ? '_manual' : '';
    const promptSuffix = customPrompt.trim() ? `_prompt_${customPrompt.trim().replace(/[^a-zA-Z0-9]/g, '_')}` : '';
    const modelSuffix = `_model_${selectedModel}`;
    const clipsSuffix = `_clips_${targetClipCount}`;
    const cacheKey = videoId ? `cheat_clip_cache_${videoId}_${durationPref}${modelSuffix}${clipsSuffix}${promptSuffix}${rangeSuffix}${manualSuffix}` : null;

    if (cacheKey) {
      const cachedData = localStorage.getItem(cacheKey);
      if (cachedData) {
        try {
          const parsedData: AnalyzeResponse = JSON.parse(cachedData);

          setLoading(true);
          setError(null);
          setResult(null);
          setActiveClip(null);
          setCurrentStep(1);
          setStepProgress({ 1: 100, 2: 0, 3: 0, 4: 0 });
          setOverallProgress(25);
          setLoadingDetails('Checking cache... Found matching clip analysis in memory!');

          // Fast progress stepper transitions for cached data (premium responsive feel)
          await new Promise(r => setTimeout(r, 300));
          setCurrentStep(2);
          setStepProgress({ 1: 100, 2: 100, 3: 0, 4: 0 });
          setOverallProgress(50);
          setLoadingDetails('Loading cached audience interest heatmap points...');
          await new Promise(r => setTimeout(r, 300));
          setCurrentStep(3);
          setStepProgress({ 1: 100, 2: 100, 3: 100, 4: 0 });
          setOverallProgress(75);
          setLoadingDetails(subtitlesSource === 'manual' ? 'Loading manual subtitles and transcript...' : 'Loading native subtitles and transcript...');
          await new Promise(r => setTimeout(r, 300));
          setCurrentStep(4);
          setStepProgress({ 1: 100, 2: 100, 3: 100, 4: 100 });
          setOverallProgress(100);
          setAiStage('Restoring Cached Highlights');
          setAiDetail('Reconstructing engagement timestamps, viral hooks, and social metadata from memory...');
          setLoadingDetails('Reconstructing viral hotspots...');
          await new Promise(r => setTimeout(r, 250));

          setResult(parsedData);
          setLoading(false);

          // Select first clip by default
          if (parsedData.clips && parsedData.clips.length > 0) {
            setActiveClip(parsedData.clips[0]);
          }

          // Initialize player
          setTimeout(() => {
            initPlayer(parsedData.video_id);
          }, 100);

          return; // Skip server request
        } catch (e) {
          console.warn('Failed to parse cached clip data, requesting fresh analysis:', e);
        }
      }
    }

    setLoading(true);
    setError(null);
    setResult(null);
    setActiveClip(null);
    setCurrentStep(1);
    setStepProgress({ 1: 20, 2: 0, 3: 0, 4: 0 });
    setOverallProgress(5);
    setAiStage('Initializing Analysis Pipeline');
    setAiDetail('Connecting to YouTube and resolving media stream metadata...');
    setActiveProcessingModel(selectedModel);
    setLoadingDetails('Connecting to YouTube...');

    let resultData: AnalyzeResponse | null = null;

    try {
      const response = await fetch('/api/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          url: url.trim(),
          duration: durationPref,
          api_key: apiKey.trim() || undefined,
          model: selectedModel,
          custom_prompt: customPrompt.trim() || undefined,
          range_start: rangeStartSecs,
          range_end: rangeEndSecs,
          subtitles: subtitlesSource === 'manual' ? manualSubtitlesContent : undefined,
          subtitles_filename: subtitlesSource === 'manual' ? manualSubtitlesFileName : undefined,
          target_clip_count: targetClipCount,
        }),
      });

      if (!response.body) throw new Error('No response stream from server.');

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let streamDone = false;

      while (!streamDone) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        // SSE events are separated by double newlines
        const parts = buffer.split('\n\n');
        buffer = parts.pop() ?? '';

        for (const part of parts) {
          for (const line of part.split('\n')) {
            if (!line.startsWith('data: ')) continue;
            let event: any;
            try { event = JSON.parse(line.slice(6)); } catch { continue; }

            if (event.error) {
              throw new Error(event.error);
            } else if (event.done) {
              resultData = event.result as AnalyzeResponse;
              setStepProgress({ 1: 100, 2: 100, 3: 100, 4: 100 });
              setOverallProgress(100);
              streamDone = true;
              break;
            } else {
              if (event.step !== undefined) {
                const s = Number(event.step);
                setCurrentStep(s);
                setStepProgress(prev => {
                  const updated = { ...prev };
                  for (let prevStep = 1; prevStep < s; prevStep++) {
                    updated[prevStep] = 100;
                  }
                  if (event.step_progress !== undefined) {
                    updated[s] = Math.max(updated[s] || 0, Number(event.step_progress));
                  }
                  return updated;
                });
              }
              if (event.overall_progress !== undefined) {
                setOverallProgress(Number(event.overall_progress));
              }
              if (event.stage) {
                setAiStage(event.stage);
              }
              if (event.detail) {
                setAiDetail(event.detail);
              }
              if (event.model) {
                setActiveProcessingModel(event.model);
              }
              if (event.message) {
                setLoadingDetails(event.message);
              }
            }
          }
          if (streamDone) break;
        }
      }

      // Flush remaining data in decoder and parse trailing buffer
      buffer += decoder.decode();
      if (!resultData && buffer.trim()) {
        const parts = buffer.split('\n\n');
        for (const part of parts) {
          for (const line of part.split('\n')) {
            if (!line.startsWith('data: ')) continue;
            try {
              const event = JSON.parse(line.slice(6));
              if (event.error) throw new Error(event.error);
              if (event.done && event.result) {
                resultData = event.result as AnalyzeResponse;
                setStepProgress({ 1: 100, 2: 100, 3: 100, 4: 100 });
                setOverallProgress(100);
                break;
              }
            } catch (err: any) {
              if (err.message && !err.message.includes('JSON')) throw err;
            }
          }
          if (resultData) break;
        }
      }

      if (!resultData) {
        throw new Error('Connection to the server was closed before analysis completed. On mobile devices, ensure your browser screen stays awake and network is stable, then try again.');
      }

      // Cache the successful response safely (handling mobile Safari quota limits)
      if (resultData.video_id) {
        try {
          const promptSuffix = customPrompt.trim() ? `_prompt_${customPrompt.trim().replace(/[^a-zA-Z0-9]/g, '_')}` : '';
          const modelSuffix = `_model_${selectedModel}`;
          const clipsSuffix = `_clips_${targetClipCount}`;
          const targetCacheKey = `cheat_clip_cache_${resultData.video_id}_${durationPref}${modelSuffix}${clipsSuffix}${promptSuffix}${rangeSuffix}${manualSuffix}`;
          const tsKey = `cheat_clip_ts_${resultData.video_id}_${durationPref}${modelSuffix}${clipsSuffix}${promptSuffix}${rangeSuffix}${manualSuffix}`;
          localStorage.setItem(targetCacheKey, JSON.stringify(resultData));
          localStorage.setItem(tsKey, new Date().toISOString());
          refreshHistory();
        } catch (storageErr) {
          console.warn('Could not cache analysis to localStorage (likely quota limit on mobile device):', storageErr);
        }
      }

      setResult(resultData);
      setLoading(false);

      if (resultData.clips?.length > 0) setActiveClip(resultData.clips[0]);
      setTimeout(() => initPlayer(resultData!.video_id), 100);

      // Scroll smoothly to the dashboard so results are immediately visible
      setTimeout(() => {
        const dashboard = document.querySelector('.dashboard-grid');
        if (dashboard) {
          dashboard.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      }, 250);

    } catch (err: any) {
      setError(err.message || 'An unexpected error occurred during analysis.');
      setLoading(false);
    }
  };

  const formatSeconds = (secs: number) => {
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = Math.floor(secs % 60);
    if (h > 0) {
      return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    }
    return `${m}:${s.toString().padStart(2, '0')}`;
  };

  const extractHashtagsAndText = (summary: string) => {
    if (!summary) return { text: '', hashtags: [] as string[] };
    const hashtagRegex = /#\w+/g;
    const hashtags = (summary.match(hashtagRegex) || []).map(tag => tag.toLowerCase());
    const text = summary.replace(hashtagRegex, '').replace(/\s+/g, ' ').trim();
    return { text, hashtags };
  };

  const handleCopyText = (text: string, label: string) => {
    navigator.clipboard.writeText(text).then(() => {
      setToastMessage(`Copied ${label} to clipboard!`);
      setTimeout(() => {
        setToastMessage(null);
      }, 3000);
    });
  };

  const handleRefreshPlayer = () => {
    if (result) {
      initPlayer(result.video_id, true);
    }
  };

  const handleCopyClip = (clip: ViralClip, e: React.MouseEvent) => {
    e.stopPropagation(); // Prevent card trigger
    let copyText = `CLIP: ${clip.title}
Timestamp: ${formatSeconds(clip.start_time)} - ${formatSeconds(clip.end_time)}
Virality Score: ${clip.virality_score}%

Transcript:
"${clip.transcript}"`;

    if (clip.key_quotes && clip.key_quotes.length > 0) {
      copyText += `\n\nKey Quotes:\n` + clip.key_quotes.map(q => `“${q}”`).join('\n');
    }
    if (clip.title_suggestion) {
      copyText += `\n\nTitle Suggestion: ${clip.title_suggestion}`;
    }
    if (clip.caption_suggestion) {
      const captionText = (() => {
        const lowercaseHashtags = (clip.hashtag_suggestion || '').toLowerCase();
        if (!lowercaseHashtags) return clip.caption_suggestion;
        if (clip.caption_suggestion.toLowerCase().includes(lowercaseHashtags)) return clip.caption_suggestion;
        return `${clip.caption_suggestion} ${lowercaseHashtags}`;
      })();
      copyText += `\n\nCaption Suggestion: ${captionText}`;
    }

    navigator.clipboard.writeText(copyText).then(() => {
      setToastMessage(`Copied "${clip.title}" details to clipboard!`);
      setTimeout(() => {
        setToastMessage(null);
      }, 3000);
    });
  };

  const handleExportJSON = () => {
    if (!result) return;
    const dataStr = "data:text/json;charset=utf-8," + encodeURIComponent(JSON.stringify(result.clips, null, 2));
    const downloadAnchor = document.createElement('a');
    downloadAnchor.setAttribute("href", dataStr);
    downloadAnchor.setAttribute("download", `cheat_clip_${result.video_id}.json`);
    document.body.appendChild(downloadAnchor);
    downloadAnchor.click();
    downloadAnchor.remove();

    setToastMessage("Downloaded clips as JSON successfully!");
    setTimeout(() => setToastMessage(null), 3000);
  };

  const handleExportSRT = () => {
    if (!result || !result.transcript) {
      setToastMessage("No transcript available to export.");
      setTimeout(() => setToastMessage(null), 3000);
      return;
    }
    
    const formatSRTTime = (secs: number): string => {
      const h = Math.floor(secs / 3600);
      const m = Math.floor((secs % 3600) / 60);
      const s = Math.floor(secs % 60);
      const ms = Math.floor((secs % 1) * 1000);
      return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')},${ms.toString().padStart(3, '0')}`;
    };

    let srtText = '';
    result.transcript.forEach((line, index) => {
      srtText += `${index + 1}\n`;
      srtText += `${formatSRTTime(line.start)} --> ${formatSRTTime(line.end)}\n`;
      srtText += `${line.text}\n\n`;
    });

    const dataStr = "data:text/plain;charset=utf-8," + encodeURIComponent(srtText);
    const downloadAnchor = document.createElement('a');
    downloadAnchor.setAttribute("href", dataStr);
    downloadAnchor.setAttribute("download", `cheat_clip_${result.video_id}.srt`);
    document.body.appendChild(downloadAnchor);
    downloadAnchor.click();
    downloadAnchor.remove();

    setToastMessage("Downloaded subtitles as SRT successfully!");
    setTimeout(() => setToastMessage(null), 3000);
  };

  const handleCopyAllMarkdown = () => {
    if (!result) return;
    let md = `# Viral Clips from "${result.title}"\n\n`;
    md += `**Overall Summary**: ${result.summary}\n\n`;
    result.clips.forEach((clip, index) => {
      md += `## ${index + 1}. ${clip.title} (${clip.virality_score}% Virality)\n`;
      md += `- **Timestamp**: ${formatSeconds(clip.start_time)} - ${formatSeconds(clip.end_time)} (Duration: ${formatSeconds(clip.end_time - clip.start_time)})\n`;
      if (clip.key_quotes && clip.key_quotes.length > 0) {
        md += `- **Key Quotes**:\n`;
        clip.key_quotes.forEach(q => md += `  - *"${q}"*\n`);
      }
      if (clip.title_suggestion) {
        md += `- **Title Suggestion**: ${clip.title_suggestion}\n`;
      }
      if (clip.caption_suggestion) {
        const captionText = (() => {
          const lowercaseHashtags = (clip.hashtag_suggestion || '').toLowerCase();
          if (!lowercaseHashtags) return clip.caption_suggestion;
          if (clip.caption_suggestion.toLowerCase().includes(lowercaseHashtags)) return clip.caption_suggestion;
          return `${clip.caption_suggestion} ${lowercaseHashtags}`;
        })();
        md += `- **Caption Suggestion**: ${captionText}\n`;
      }
      md += `- **Transcript**:\n  > ${clip.transcript.replace(/\n/g, '\n  > ')}\n\n`;
    });

    navigator.clipboard.writeText(md).then(() => {
      setToastMessage("Copied all clips as Markdown to clipboard!");
      setTimeout(() => setToastMessage(null), 3000);
    });
  };

  // Filter clips based on query and virality filters
  const filteredClips = useMemo(() => {
    if (!result?.clips) return [];
    return result.clips.filter(clip => {
      const matchesSearch = searchQuery.trim() === '' ||
        clip.title.toLowerCase().includes(searchQuery.toLowerCase()) ||
        clip.transcript.toLowerCase().includes(searchQuery.toLowerCase());

      const matchesVirality = viralityFilter === 'all' ||
        (viralityFilter === 'high' && clip.virality_score >= 90) ||
        (viralityFilter === 'medium' && clip.virality_score < 90) ||
        (viralityFilter === 'marked' && !!markedClips[`${clip.start_time}_${clip.end_time}`]);

      return matchesSearch && matchesVirality;
    });
  }, [result?.clips, searchQuery, viralityFilter, markedClips]);

  // Sort the filtered clips based on selected sortBy
  const sortedClips = useMemo(() => {
    return [...filteredClips].sort((a, b) => {
      if (sortBy === 'virality') {
        return b.virality_score - a.virality_score;
      } else if (sortBy === 'time') {
        return a.start_time - b.start_time;
      } else if (sortBy === 'duration') {
        return (b.end_time - b.start_time) - (a.end_time - a.start_time);
      } else if (sortBy === 'marked') {
        const aMarked = !!markedClips[`${a.start_time}_${a.end_time}`];
        const bMarked = !!markedClips[`${b.start_time}_${b.end_time}`];
        if (aMarked && !bMarked) return -1;
        if (!aMarked && bMarked) return 1;
        return b.virality_score - a.virality_score;
      }
      return 0;
    });
  }, [filteredClips, sortBy, markedClips]);

  // Smooth scroll active clip card into view in the sidebar list
  useEffect(() => {
    if (activeClip && sortedClips) {
      const index = sortedClips.findIndex(
        c => c.start_time === activeClip.start_time && c.end_time === activeClip.end_time
      );
      if (index !== -1) {
        const element = document.getElementById(`clip-card-${index}`);
        if (element) {
          element.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
      }
    }
  }, [activeClip, sortedClips]);

  // Find current subtitle line
  const currentSubtitle = result?.transcript?.find(
    line => currentTime >= line.start && currentTime <= line.end
  );

  return (
    <div className="app-container">
      {/* Toast Notification */}
      {toastMessage && (
        <div className="toast-msg">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
            <polyline points="20 6 9 17 4 12"></polyline>
          </svg>
          {toastMessage}
        </div>
      )}

      {/* Header Area */}
      <header className="app-header">
        <div className="header-logo">
          <span style={{ fontSize: '2.5rem' }}>⚡</span>
          <div>
            <h1 className="text-gradient logo-title">CHEAT CLIP</h1>
            <p className="header-subtitle">AI-Powered YouTube Viral Hook & Hotspot Finder</p>
          </div>
        </div>
        <div className="header-nav">
          <a
            href="https://tako.id/johansa"
            target="_blank"
            rel="noopener noreferrer"
            className="glowing-btn"
            style={{
              padding: '0.5rem 1rem',
              fontSize: '0.8rem',
              borderRadius: '8px',
              textDecoration: 'none',
              boxShadow: '0 4px 12px rgba(255, 94, 58, 0.2)',
              background: 'linear-gradient(135deg, var(--secondary) 0%, #f43f5e 100%)'
            }}
          >
            🐈‍⬛ Support This Project
          </a>
        </div>
      </header>

      {/* Main Form controls panel */}
      <section className="glass-panel" style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
        <form onSubmit={handleAnalyze} style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
          <div className="form-main-input-row">
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
              <label style={{ fontSize: '0.875rem', fontWeight: 600, color: 'var(--text-secondary)' }}>YouTube Video URL</label>
              <input
                id="youtube-url-input"
                type="text"
                className="form-input"
                placeholder="Paste video link here (e.g. https://www.youtube.com/watch?v=... or shorts, youtu.be)"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                disabled={loading}
                required
              />
            </div>
            <button
              id="analyze-btn"
              type="submit"
              className="glowing-btn"
              disabled={loading || !url.trim()}
              style={{ height: '48px', padding: '0 2.5rem' }}
            >
              {loading ? (
                <>
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" className="spinner-icon" style={{ animation: 'spin 1s linear infinite' }}>
                    <circle cx="12" cy="12" r="10" strokeDasharray="32" strokeDashoffset="8"></circle>
                  </svg>
                  Processing...
                </>
              ) : (
                <>
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                    <polygon points="5 3 19 12 5 21 5 3"></polygon>
                  </svg>
                  Hack Clips
                </>
              )}
            </button>
          </div>

          <div className="form-settings-grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: '1.5rem' }}>
            {/* Card 1: AI Engine Configuration */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem', padding: '1.25rem', borderRadius: '12px', background: 'rgba(255, 255, 255, 0.02)', border: '1px solid rgba(255, 255, 255, 0.04)' }}>
              <h3 style={{ fontSize: '0.9rem', color: 'var(--primary)', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', display: 'flex', alignItems: 'center', gap: '0.4rem', marginBottom: '0.25rem' }}>
                🤖 AI Engine Settings
              </h3>
              
              {/* API Key input — required */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                <label style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.875rem', fontWeight: 600, color: 'var(--text-secondary)' }}>
                  <span>
                    Gemini API Key
                    <span style={{ marginLeft: '0.4rem', fontSize: '0.7rem', fontWeight: 700, color: '#f87171', background: 'rgba(239,68,68,0.12)', border: '1px solid rgba(239,68,68,0.3)', borderRadius: '4px', padding: '0.1rem 0.35rem', letterSpacing: '0.04em' }}>REQUIRED</span>
                  </span>
                  <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                    <a
                      href="https://aistudio.google.com/"
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{ color: 'var(--primary)', textDecoration: 'none', fontSize: '0.75rem', fontWeight: 600, transition: 'var(--transition-smooth)' }}
                      className="action-link-btn"
                    >
                      🔑 Get free key
                    </a>
                    <span style={{ color: 'rgba(255,255,255,0.15)', fontSize: '0.75rem' }}>|</span>
                    <span
                      onClick={() => setShowApiKey(!showApiKey)}
                      style={{ cursor: 'pointer', color: 'var(--primary)', fontSize: '0.75rem' }}
                    >
                      {showApiKey ? 'Hide key' : 'Show key'}
                    </span>
                  </div>
                </label>
                <input
                  id="gemini-key-input"
                  type={showApiKey ? 'text' : 'password'}
                  className={`form-input${!apiKey.trim() ? ' input-error-highlight' : ''}`}
                  placeholder="Paste your Gemini API Key here — get one free at aistudio.google.com"
                  value={apiKey}
                  onChange={(e) => {
                    const val = e.target.value;
                    setApiKey(val);
                    localStorage.setItem('cheat_clip_gemini_api_key', val);
                    if (val.trim()) setError(null);
                  }}
                  disabled={loading}
                  style={{ height: '42px' }}
                />
                {!apiKey.trim() && (
                  <span style={{ fontSize: '0.75rem', color: '#f87171', display: 'flex', alignItems: 'center', gap: '0.3rem' }}>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" /></svg>
                    Required — saved locally in browser.
                  </span>
                )}
              </div>

              {/* AI Model Selection */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                <label style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.875rem', fontWeight: 600, color: 'var(--text-secondary)' }}>
                  <span>AI Model Selection</span>
                  {loadingModels && (
                    <span style={{ fontSize: '0.72rem', color: 'var(--primary)', animation: 'pulse 1.5s infinite ease-in-out' }}>
                      ⌛ Fetching available...
                    </span>
                  )}
                </label>
                <select
                  className="form-input"
                  value={selectedModel}
                  onChange={(e) => {
                    setSelectedModel(e.target.value);
                    localStorage.setItem('cheat_clip_selected_model', e.target.value);
                  }}
                  disabled={loading}
                  style={{ padding: '0.6rem 1rem', fontSize: '0.875rem', height: '42px', cursor: 'pointer', appearance: 'auto', background: 'rgba(255,255,255,0.03)', border: '1px solid var(--border-color)', borderRadius: '8px' }}
                >
                  {availableModels.length > 0 ? (
                    availableModels.map((m) => (
                      <option key={m} value={m} style={{ background: '#0d1324', color: '#fff' }}>
                        {m}
                      </option>
                    ))
                  ) : (
                    <>
                      <option value="gemini-2.5-flash" style={{ background: '#0d1324', color: '#fff' }}>gemini-2.5-flash (Fast & recommended - Free tier friendly)</option>
                      <option value="gemini-2.5-flash-lite" style={{ background: '#0d1324', color: '#fff' }}>gemini-2.5-flash-lite (Ultra-fast & lightweight)</option>
                      <option value="gemini-2.0-flash" style={{ background: '#0d1324', color: '#fff' }}>gemini-2.0-flash (Fast & responsive)</option>
                      <option value="gemini-2.0-flash-lite" style={{ background: '#0d1324', color: '#fff' }}>gemini-2.0-flash-lite (Lightweight flash)</option>
                      <option value="gemini-2.5-pro" style={{ background: '#0d1324', color: '#fff' }}>gemini-2.5-pro (Creative & complex - High quota)</option>
                    </>
                  )}
                </select>
                <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)', lineHeight: 1.4, marginTop: '0.2rem' }}>
                  💡 <strong>Free Tier Resilience:</strong> If a model encounters quota limits or errors, Cheat Clip automatically tries all available Flash models (<strong>gemini-2.5-flash, 2.5-flash-lite, 2.0-flash, 2.0-flash-lite</strong>) until clips are successfully found!
                </span>
              </div>
            </div>

            {/* Card 2: Clip Parameters & Focus */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem', padding: '1.25rem', borderRadius: '12px', background: 'rgba(255, 255, 255, 0.02)', border: '1px solid rgba(255, 255, 255, 0.04)' }}>
              <h3 style={{ fontSize: '0.9rem', color: 'var(--secondary)', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', display: 'flex', alignItems: 'center', gap: '0.4rem', marginBottom: '0.25rem' }}>
                ⚡ Clip Customization
              </h3>

              {/* Preferred Duration Selector */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                <label style={{ fontSize: '0.875rem', fontWeight: 600, color: 'var(--text-secondary)' }}>Target Clip Duration</label>
                <div className="duration-selector" id="duration-selector-group">
                  <button
                    type="button"
                    className={`duration-btn ${durationPref === '15s' ? 'active' : ''}`}
                    onClick={() => setDurationPref('15s')}
                    disabled={loading}
                  >
                    ⚡ 15s clips
                  </button>
                  <button
                    type="button"
                    className={`duration-btn ${durationPref === '30s' ? 'active' : ''}`}
                    onClick={() => setDurationPref('30s')}
                    disabled={loading}
                  >
                    🔥 30s clips
                  </button>
                  <button
                    type="button"
                    className={`duration-btn ${durationPref === '60s' ? 'active' : ''}`}
                    onClick={() => setDurationPref('60s')}
                    disabled={loading}
                  >
                    🎬 60s clips
                  </button>
                </div>
              </div>

              {/* Focus Prompt Search Keyword */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                <label style={{ fontSize: '0.875rem', fontWeight: 600, color: 'var(--text-secondary)' }}>
                  Find Specific Moments <span style={{ color: 'var(--text-muted)', fontSize: '0.75rem', fontWeight: 400 }}>(Optional)</span>
                </label>
                <input
                  type="text"
                  className="form-input"
                  placeholder="e.g., look for funny moments, controversial topics, technical explanation"
                  value={customPrompt}
                  onChange={(e) => setCustomPrompt(e.target.value)}
                  disabled={loading}
                  style={{ height: '42px' }}
                />
              </div>

              {/* Target Clip Count Slider */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', marginTop: '0.25rem' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <label style={{ fontSize: '0.875rem', fontWeight: 600, color: 'var(--text-secondary)' }}>
                    Target Clip Count
                  </label>
                  <span style={{
                    fontSize: '0.8rem',
                    fontWeight: 700,
                    color: 'var(--secondary)',
                    background: 'rgba(255, 94, 58, 0.12)',
                    border: '1px solid rgba(255, 94, 58, 0.3)',
                    borderRadius: '6px',
                    padding: '0.1rem 0.5rem'
                  }}>
                    ≈ {targetClipCount} Clips
                  </span>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                  <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', width: '10px' }}>1</span>
                  <input
                    type="range"
                    min="1"
                    max="50"
                    value={targetClipCount}
                    onChange={(e) => {
                      const val = Number(e.target.value);
                      setTargetClipCount(val);
                      localStorage.setItem('cheat_clip_target_clip_count', String(val));
                    }}
                    disabled={loading}
                    style={{
                      flex: 1,
                      height: '6px',
                      borderRadius: '3px',
                      background: 'rgba(255, 255, 255, 0.1)',
                      outline: 'none',
                      cursor: 'pointer',
                      accentColor: 'var(--secondary)'
                    }}
                  />
                  <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', width: '20px', textAlign: 'right' }}>50</span>
                </div>
                <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)', lineHeight: 1.3 }}>
                  💡 The AI will target around <strong>{targetClipCount}</strong> clips, adjusting dynamically (e.g., {targetClipCount <= 5 ? `${Math.max(1, targetClipCount - 1)}-${targetClipCount + 2}` : targetClipCount <= 10 ? `${Math.max(1, targetClipCount - 2)}-${targetClipCount + 3}` : `${targetClipCount - 5}-${targetClipCount + 5}`} clips) to ensure maximum quality without hard constraints.
                </span>
              </div>
            </div>
          </div>

          {/* Subtitles Source Section */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem', borderTop: '1px solid var(--border-color)', paddingTop: '1.25rem', marginTop: '0.5rem' }}>
            <label style={{ fontSize: '0.875rem', fontWeight: 600, color: 'var(--text-secondary)' }}>Subtitles Source</label>
            <div style={{ display: 'flex', gap: '1.5rem', flexWrap: 'wrap', alignItems: 'center' }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '0.9rem', cursor: 'pointer' }}>
                <input
                  type="radio"
                  name="subtitlesSource"
                  checked={subtitlesSource === 'youtube'}
                  onChange={() => setSubtitlesSource('youtube')}
                  style={{ accentColor: 'var(--primary)' }}
                  disabled={loading}
                />
                Auto-fetch from YouTube
              </label>
              <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '0.9rem', cursor: 'pointer' }}>
                <input
                  type="radio"
                  name="subtitlesSource"
                  checked={subtitlesSource === 'manual'}
                  onChange={() => setSubtitlesSource('manual')}
                  style={{ accentColor: 'var(--primary)' }}
                  disabled={loading}
                />
                Upload Custom Subtitles (.srt, .txt)
              </label>

              {subtitlesSource === 'manual' && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                  <input
                    type="file"
                    accept=".srt,.txt"
                    id="manual-subtitle-file"
                    style={{ display: 'none' }}
                    disabled={loading}
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      if (!file) return;
                      setManualSubtitlesFileName(file.name);
                      const reader = new FileReader();
                      reader.onload = (evt) => {
                        const text = evt.target?.result as string;
                        setManualSubtitlesContent(text);
                        setToastMessage(`Loaded: ${file.name}`);
                        setTimeout(() => setToastMessage(null), 3000);
                      };
                      reader.readAsText(file);
                    }}
                  />
                  <label
                    htmlFor="manual-subtitle-file"
                    className="form-input"
                    style={{
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: '0.5rem',
                      padding: '0.5rem 1rem',
                      cursor: 'pointer',
                      background: 'rgba(255, 255, 255, 0.05)',
                      border: '1px solid var(--border-color)',
                      borderRadius: '8px',
                      fontSize: '0.85rem',
                      width: 'auto',
                      color: 'var(--text-primary)',
                      transition: 'var(--transition-smooth)'
                    }}
                  >
                    📂 Choose SRT/TXT File
                  </label>
                  {manualSubtitlesFileName && (
                    <span style={{ fontSize: '0.85rem', color: 'var(--primary)', fontWeight: '500' }}>
                      📄 {manualSubtitlesFileName}
                    </span>
                  )}
                </div>
              )}
            </div>

            {subtitlesSource === 'youtube' && (
              <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', opacity: 0.8, display: 'block', marginTop: '0.15rem', lineHeight: '1.4' }}>
                💡 <strong>Hosting on Vercel?</strong> Serverless providers can get blocked when fetching auto-generated YouTube transcripts. If auto-fetch fails, download the subtitles manually using <a href="https://downsub.com/" target="_blank" rel="noopener noreferrer" style={{ color: 'var(--secondary)', textDecoration: 'underline', fontWeight: '500' }}>downsub.com</a> and upload them using the <strong>Upload Custom Subtitles</strong> option.
              </span>
            )}
          </div>

          {/* Custom Search Range Section */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem', borderTop: '1px solid var(--border-color)', paddingTop: '1.25rem', marginTop: '0.5rem' }}>
            <label style={{ fontSize: '0.875rem', fontWeight: 600, color: 'var(--text-secondary)' }}>Analysis Range</label>
            <div style={{ display: 'flex', gap: '1.5rem', flexWrap: 'wrap', alignItems: 'center' }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '0.9rem', cursor: 'pointer' }}>
                <input
                  type="radio"
                  name="rangeType"
                  checked={rangeType === 'entire'}
                  onChange={() => setRangeType('entire')}
                  style={{ accentColor: 'var(--primary)' }}
                  disabled={loading}
                />
                Entire Video
              </label>
              <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '0.9rem', cursor: 'pointer' }}>
                <input
                  type="radio"
                  name="rangeType"
                  checked={rangeType === 'custom'}
                  onChange={() => setRangeType('custom')}
                  style={{ accentColor: 'var(--primary)' }}
                  disabled={loading}
                />
                Custom Range
              </label>

              {rangeType === 'custom' && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
                  <input
                    type="text"
                    className="form-input"
                    placeholder="Start (e.g. 29:00)"
                    value={customRangeStart}
                    onChange={(e) => setCustomRangeStart(e.target.value)}
                    style={{ width: '150px', padding: '0.5rem 0.75rem', fontSize: '0.875rem' }}
                    disabled={loading}
                  />
                  <span style={{ color: 'var(--text-muted)' }}>to</span>
                  <input
                    type="text"
                    className="form-input"
                    placeholder="End (e.g. 31:15)"
                    value={customRangeEnd}
                    onChange={(e) => setCustomRangeEnd(e.target.value)}
                    style={{ width: '150px', padding: '0.5rem 0.75rem', fontSize: '0.875rem' }}
                    disabled={loading}
                  />
                </div>
              )}
            </div>
            {rangeType === 'custom' && (
              <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                Supports format like <b>MM:SS</b> (e.g., 29:00), <b>HH:MM:SS</b>, or raw seconds (e.g., 600).
              </span>
            )}
          </div>
        </form>

        {/* History toggle row */}
        {history.length > 0 && (
          <div style={{ borderTop: '1px solid var(--border-color)', paddingTop: '1rem' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', cursor: 'pointer' }} onClick={() => setShowHistory(h => !h)}>
              <span style={{ fontSize: '0.875rem', fontWeight: 600, color: 'var(--text-secondary)', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                <span style={{ fontSize: '1rem' }}>🕓</span>
                Previously Analyzed Videos
                <span style={{ background: 'rgba(var(--primary-rgb, 168 85 247) / 0.15)', color: 'var(--primary)', fontSize: '0.7rem', fontWeight: 'bold', padding: '0.15rem 0.5rem', borderRadius: '20px', border: '1px solid rgba(168,85,247,0.3)' }}>
                  {history.length}
                </span>
              </span>
              <div style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
                {showHistory && (
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); clearAllHistory(); }}
                    style={{ fontSize: '0.75rem', color: '#ef4444', background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.25)', borderRadius: '6px', padding: '0.25rem 0.6rem', cursor: 'pointer' }}
                  >
                    🗑 Clear All
                  </button>
                )}
                <span style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>{showHistory ? '▲' : '▼'}</span>
              </div>
            </div>

            {showHistory && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', marginTop: '0.75rem', maxHeight: '300px', overflowY: 'auto', paddingRight: '0.25rem' }}>
                {history.map((entry) => (
                  <div
                    key={`${entry.video_id}_${entry.duration_pref}`}
                    onClick={() => loadFromHistory(entry)}
                    style={{
                      display: 'flex',
                      gap: '0.75rem',
                      alignItems: 'center',
                      padding: '0.6rem 0.75rem',
                      borderRadius: '8px',
                      background: 'rgba(255,255,255,0.03)',
                      border: '1px solid var(--border-color)',
                      cursor: 'pointer',
                      transition: 'var(--transition-smooth)'
                    }}
                    onMouseEnter={e => (e.currentTarget.style.background = 'rgba(168,85,247,0.08)')}
                    onMouseLeave={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.03)')}
                  >
                    <img
                      src={entry.thumbnail}
                      alt=""
                      style={{ width: '72px', height: '42px', objectFit: 'cover', borderRadius: '5px', flexShrink: 0, background: '#111' }}
                    />
                    <div style={{ flex: 1, overflow: 'hidden' }}>
                      <div style={{ fontSize: '0.875rem', fontWeight: 600, color: 'var(--text-primary)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {entry.title}
                      </div>
                      <div style={{ display: 'flex', gap: '0.75rem', marginTop: '0.2rem', fontSize: '0.73rem', color: 'var(--text-muted)', flexWrap: 'wrap' }}>
                        <span>🎬 {entry.clip_count} clips</span>
                        <span>⏱ {entry.duration_pref}</span>
                        <span>🕓 {formatRelativeTime(entry.analyzed_at)}</span>
                      </div>
                      <div style={{ marginTop: '0.15rem', fontSize: '0.68rem', color: 'var(--text-muted)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        <a
                          href={entry.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          onClick={(e) => e.stopPropagation()}
                          style={{ color: 'var(--primary)', textDecoration: 'none', opacity: 0.75 }}
                        >
                          🔗 {entry.url}
                        </a>
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={(e) => deleteHistoryEntry(entry, e)}
                      style={{ flexShrink: 0, background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.2)', color: '#ef4444', borderRadius: '6px', padding: '0.3rem 0.5rem', cursor: 'pointer', fontSize: '0.75rem' }}
                      title="Remove from history"
                    >
                      ✕
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </section>

      {/* Error state */}
      {error && (
        <section className="glass-panel" style={{ borderColor: 'rgba(239, 68, 68, 0.3)', background: 'rgba(239, 68, 68, 0.05)' }}>
          <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'center' }}>
            <span style={{ fontSize: '1.5rem', color: '#ef4444' }}>⚠️</span>
            <div>
              <h4 style={{ color: '#ef4444' }}>Analysis Failed</h4>
              {error.toLowerCase().includes("no subtitles") ? (
                <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginTop: '0.25rem', lineHeight: '1.5' }}>
                  No subtitles could be retrieved for this video. Subtitles might be disabled, or the video may be age-restricted or private.
                  <br /><br />
                  💡 <strong>Tip for Serverless Deployment (Vercel):</strong> Serverless hosting providers can get blocked when fetching auto-generated YouTube transcripts. To resolve this, you can download the subtitles manually using a tool like <a href="https://downsub.com/" target="_blank" rel="noopener noreferrer" style={{ color: 'var(--secondary)', textDecoration: 'underline', fontWeight: '500' }}>downsub.com</a>, upload the file via the <strong>Upload custom subtitle</strong> settings above, and try analyzing the video again.
                </p>
              ) : (
                <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginTop: '0.25rem' }}>{error}</p>
              )}
            </div>
          </div>
        </section>
      )}

      {/* Loading Steps state with Real Progress Bars & Cognitive AI Diagnostics */}
      {loading && (
        <section
          ref={loadingSectionRef}
          id="loading-progress-section"
          className="glass-panel"
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: '1.5rem',
            alignItems: 'center',
            padding: '2.5rem 1.75rem',
            scrollMarginTop: '2.5rem'
          }}
        >
          <div style={{ width: '100%', maxWidth: '620px' }}>
            <div style={{ textAlign: 'center', marginBottom: '1.75rem' }}>
              <h3 style={{ marginBottom: '0.4rem', fontSize: '1.4rem' }} className="text-gradient">
                Decoding Video Engagement
              </h3>
              <p style={{ fontSize: '0.82rem', color: 'var(--text-muted)', margin: 0 }}>
                Multimodal AI pipeline extracting viral retention spikes and hook sequences
              </p>
            </div>

            {/* Master Progress Bar */}
            <div style={{ marginBottom: '2rem', padding: '0.85rem 1rem', background: 'rgba(255, 255, 255, 0.02)', borderRadius: '10px', border: '1px solid rgba(255, 255, 255, 0.05)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.45rem' }}>
                <span style={{ fontSize: '0.78rem', fontWeight: 600, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                  Pipeline Overall Completion
                </span>
                <span style={{ fontSize: '0.88rem', fontWeight: 700, color: 'var(--primary)', fontVariantNumeric: 'tabular-nums' }}>
                  {overallProgress}%
                </span>
              </div>
              <div style={{ height: '7px', background: 'rgba(255, 255, 255, 0.05)', borderRadius: '4px', overflow: 'hidden' }}>
                <div
                  style={{
                    height: '100%',
                    background: 'linear-gradient(90deg, var(--primary) 0%, var(--secondary) 100%)',
                    width: `${overallProgress}%`,
                    transition: 'width 0.4s ease'
                  }}
                />
              </div>
            </div>

            {/* Stepper with Individual Progress Bars */}
            <div className="stepper-container">
              {/* Step 1 */}
              <div className={`step-item ${currentStep === 1 ? 'active' : currentStep > 1 ? 'completed' : ''}`}>
                <div className="step-circle">{currentStep > 1 ? '✓' : '1'}</div>
                <div className="step-content">
                  <div className="step-header-row">
                    <span className="step-label">Extracting URL & YouTube video details</span>
                    <span className="step-percentage">
                      {currentStep > 1 ? '100%' : `${stepProgress[1] || 0}%`}
                    </span>
                  </div>
                  <div className="step-mini-bar-track">
                    <div
                      className="step-mini-bar-fill"
                      style={{ width: `${currentStep > 1 ? 100 : (stepProgress[1] || 0)}%` }}
                    />
                  </div>
                  {currentStep === 1 && (
                    <span className="step-subtext">Resolving video stream metadata, duration bounds, and title...</span>
                  )}
                </div>
              </div>

              {/* Step 2 */}
              <div className={`step-item ${currentStep === 2 ? 'active' : currentStep > 2 ? 'completed' : ''}`}>
                <div className="step-circle">{currentStep > 2 ? '✓' : '2'}</div>
                <div className="step-content">
                  <div className="step-header-row">
                    <span className="step-label">Scraping audience retention heatmap data</span>
                    <span className="step-percentage">
                      {currentStep > 2 ? '100%' : currentStep === 2 ? `${stepProgress[2] || 0}%` : '0%'}
                    </span>
                  </div>
                  <div className="step-mini-bar-track">
                    <div
                      className="step-mini-bar-fill"
                      style={{ width: `${currentStep > 2 ? 100 : currentStep === 2 ? (stepProgress[2] || 0) : 0}%` }}
                    />
                  </div>
                  {currentStep === 2 && (
                    <span className="step-subtext">Sampling 100 audience replay points & calculating engagement distribution...</span>
                  )}
                </div>
              </div>

              {/* Step 3 */}
              <div className={`step-item ${currentStep === 3 ? 'active' : currentStep > 3 ? 'completed' : ''}`}>
                <div className="step-circle">{currentStep > 3 ? '✓' : '3'}</div>
                <div className="step-content">
                  <div className="step-header-row">
                    <span className="step-label">Retrieving subtitles & translating transcript</span>
                    <span className="step-percentage">
                      {currentStep > 3 ? '100%' : currentStep === 3 ? `${stepProgress[3] || 0}%` : '0%'}
                    </span>
                  </div>
                  <div className="step-mini-bar-track">
                    <div
                      className="step-mini-bar-fill"
                      style={{ width: `${currentStep > 3 ? 100 : currentStep === 3 ? (stepProgress[3] || 0) : 0}%` }}
                    />
                  </div>
                  {currentStep === 3 && (
                    <span className="step-subtext">Aligning dialogue timestamps and sentences for precise audio cut boundaries...</span>
                  )}
                </div>
              </div>

              {/* Step 4 */}
              <div className={`step-item ${currentStep === 4 ? 'active' : ''}`}>
                <div className="step-circle">{currentStep > 4 ? '✓' : '4'}</div>
                <div className="step-content">
                  <div className="step-header-row">
                    <span className="step-label">AI analysis & viral clip extraction</span>
                    <span className="step-percentage">
                      {currentStep === 4 ? `${stepProgress[4] || 0}%` : '0%'}
                    </span>
                  </div>
                  <div className="step-mini-bar-track">
                    <div
                      className="step-mini-bar-fill"
                      style={{ width: `${currentStep === 4 ? (stepProgress[4] || 0) : 0}%` }}
                    />
                  </div>

                  {currentStep === 4 && (
                    <div className="ai-activity-card">
                      <div className="ai-activity-topbar">
                        <div className="ai-engine-badge">
                          <span style={{ fontSize: '0.85rem' }}>⚡</span>
                          <span>AI Cognitive Engine</span>
                          {activeProcessingModel && (
                            <span style={{ opacity: 0.85, fontWeight: 500 }}>({activeProcessingModel})</span>
                          )}
                        </div>
                        <div className="ai-timer-badge">
                          <span>⏱️ {loadingElapsedTime}s elapsed</span>
                        </div>
                      </div>

                      <div>
                        <div className="ai-stage-title">
                          <span style={{ animation: 'spin 2.5s linear infinite', display: 'inline-block' }}>🧠</span>
                          <span>{aiStage || 'Synthesizing Viral Highlights'}</span>
                        </div>
                      </div>

                      <div className="ai-stage-detail">
                        {aiDetail || loadingDetails || 'Evaluating audience drop-off gradients and sentence coherence...'}
                      </div>

                      {/* 4 Micro-phase progress pills */}
                      <div className="ai-subphases-row">
                        <div className={`ai-subphase-pill ${(stepProgress[4] || 0) >= 25 ? 'completed' : (stepProgress[4] || 0) >= 5 ? 'active' : ''}`}>
                          1. Heatmap Peaks
                        </div>
                        <div className={`ai-subphase-pill ${(stepProgress[4] || 0) >= 55 ? 'completed' : (stepProgress[4] || 0) >= 25 ? 'active' : ''}`}>
                          2. Hook Extraction
                        </div>
                        <div className={`ai-subphase-pill ${(stepProgress[4] || 0) >= 80 ? 'completed' : (stepProgress[4] || 0) >= 55 ? 'active' : ''}`}>
                          3. Virality Scoring
                        </div>
                        <div className={`ai-subphase-pill ${(stepProgress[4] || 0) >= 95 ? 'completed' : (stepProgress[4] || 0) >= 80 ? 'active' : ''}`}>
                          4. Social Metadata
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            </div>

            <div style={{ marginTop: '1.75rem', textAlign: 'center', fontSize: '0.82rem', color: 'var(--text-muted)' }}>
              <span className="pulsing-text">⚙️ {loadingDetails}</span>
            </div>
          </div>
        </section>
      )}

      {/* Dashboard Section - Video Player, Timeline, and Clip list */}
      {result && (
        <main className="dashboard-grid">
          {/* Left panel: Player + Heatmap */}
          <div className="sticky-player-panel" ref={leftPanelRef}>
            <div className="glass-panel" style={{ padding: '1.25rem', display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
              <h2 style={{ fontSize: '1.25rem', lineHeight: 1.3 }}>{result.title}</h2>

              <div id="youtube-player-container" className="video-wrapper" style={{ position: 'relative' }}>
                <div id="youtube-player"></div>
                {subtitlesSource === 'manual' && currentSubtitle && (
                  <div className="video-subtitle-overlay">
                    <span>{currentSubtitle.text}</span>
                  </div>
                )}
              </div>

              {/* Refresh Player control */}
              <div style={{ display: 'flex', marginTop: '0.25rem', marginBottom: '0.25rem' }}>
                <button
                  type="button"
                  className="form-input"
                  onClick={handleRefreshPlayer}
                  style={{
                    flex: 1,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: '0.4rem',
                    fontSize: '0.8rem',
                    padding: '0.5rem 1rem',
                    borderRadius: '8px',
                    cursor: 'pointer',
                    background: 'rgba(255, 255, 255, 0.03)',
                    border: '1px solid rgba(255, 255, 255, 0.05)',
                    color: 'var(--text-primary)'
                  }}
                >
                  🔄 Refresh Player
                </button>
              </div>

              {/* Heatmap Timeline component */}
              <HeatmapTimeline
                duration={result.duration}
                heatmap={result.heatmap}
                currentTime={currentTime}
                onSeek={handleSeek}
                activeClip={activeClip}
              />
            </div>

            {/* AI Summary card */}
            <div className="glass-panel" style={{ padding: '1.25rem', display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
              <div>
                <h3 style={{ fontSize: '1.05rem', color: 'var(--primary)', marginBottom: '0.5rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Video Summary</h3>
                {(() => {
                  const { text, hashtags } = extractHashtagsAndText(result.summary);
                  return (
                    <>
                      {hashtags.length > 0 && (
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.35rem', marginBottom: '0.75rem' }}>
                          {hashtags.map((tag, idx) => (
                            <span
                              key={idx}
                              className="summary-hashtag-highlight"
                              style={{
                                color: 'var(--accent)',
                                fontWeight: '600',
                                background: 'rgba(16, 185, 129, 0.1)',
                                padding: '2px 6px',
                                borderRadius: '4px',
                                border: '1px solid rgba(16, 185, 129, 0.2)',
                                fontSize: '0.75rem',
                                display: 'inline-block'
                              }}
                            >
                              {tag}
                            </span>
                          ))}
                        </div>
                      )}
                      <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', lineHeight: 1.5 }}>{text}</p>
                    </>
                  );
                })()}
              </div>

              <div style={{ borderTop: '1px solid var(--border-color)', paddingTop: '1rem' }}>
                <h3 style={{ fontSize: '1.05rem', color: 'var(--secondary)', marginBottom: '0.75rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                  Generated Clips Overview ({result.clips.length})
                </h3>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', maxHeight: '250px', overflowY: 'auto', paddingRight: '0.5rem' }}>
                  {result.clips.map((clip, idx) => {
                    const isSelected = activeClip?.start_time === clip.start_time && activeClip?.end_time === clip.end_time;
                    const clipKey = `${clip.start_time}_${clip.end_time}`;
                    const isMarked = !!markedClips[clipKey];
                    return (
                      <div
                        key={idx}
                        onClick={() => setActiveClip(clip)}
                        style={{
                          display: 'flex',
                          justifyContent: 'space-between',
                          alignItems: 'center',
                          fontSize: '0.8rem',
                          padding: '0.5rem',
                          borderRadius: '6px',
                          background: isSelected
                            ? 'rgba(255, 94, 58, 0.1)'
                            : 'rgba(255, 255, 255, 0.02)',
                          border: isSelected
                            ? '1px solid var(--secondary)'
                            : '1px solid transparent',
                          cursor: 'pointer',
                          transition: 'var(--transition-smooth)',
                          opacity: 1
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flex: 1, overflow: 'hidden' }}>
                          <input
                            type="checkbox"
                            checked={isMarked}
                            onChange={(e) => {
                              e.stopPropagation();
                              toggleMarkedClip(clipKey);
                            }}
                            style={{
                              width: '14px',
                              height: '14px',
                              cursor: 'pointer',
                              accentColor: 'var(--secondary)'
                            }}
                          />
                          <span style={{
                            fontWeight: isSelected ? 700 : 500,
                            color: isMarked
                              ? 'var(--secondary)'
                              : (isSelected ? 'var(--text-primary)' : 'var(--text-secondary)'),
                            whiteSpace: 'nowrap',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis'
                          }}>
                            {idx + 1}. {clip.title}
                          </span>
                        </div>
                        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '0.15rem', flexShrink: 0 }}>
                          <span style={{
                            fontSize: '0.68rem',
                            fontWeight: 700,
                            color: clip.virality_score >= 90 ? 'var(--secondary)' : 'var(--primary)',
                            background: clip.virality_score >= 90
                              ? 'rgba(255, 94, 58, 0.12)'
                              : 'rgba(168, 85, 247, 0.12)',
                            border: `1px solid ${clip.virality_score >= 90 ? 'rgba(255,94,58,0.35)' : 'rgba(168,85,247,0.35)'}`,
                            borderRadius: '4px',
                            padding: '0.05rem 0.35rem',
                            whiteSpace: 'nowrap'
                          }}>
                            🔥 {clip.virality_score}%
                          </span>
                          <span style={{
                            color: 'var(--text-muted)',
                            fontFamily: 'monospace',
                            fontSize: '0.68rem',
                            whiteSpace: 'nowrap'
                          }}>
                            {formatSeconds(clip.start_time)} – {formatSeconds(clip.end_time)}
                          </span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          </div>

          {/* Right panel: Suggested Clips scrollable list */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem', maxHeight: leftPanelHeight ? `${leftPanelHeight}px` : '80vh' }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem', borderBottom: '1px solid var(--border-color)', paddingBottom: '1.25rem' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <h2 style={{ fontSize: '1.5rem', fontFamily: 'Outfit' }}>Recommended Clips</h2>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontWeight: 'bold' }}>SORT: {sortBy.toUpperCase()}</span>
              </div>

              {/* Analysis Metadata Info Bar */}
              <div style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: '1rem',
                padding: '0.6rem 1rem',
                borderRadius: '10px',
                background: 'rgba(255, 255, 255, 0.02)',
                border: '1px solid var(--border-color)',
                fontSize: '0.8rem',
                color: 'var(--text-secondary)',
                flexWrap: 'wrap'
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                  <span style={{ fontSize: '1rem' }}>🤖</span>
                  <span>AI Model:</span>
                  <strong style={{ color: 'var(--primary)', fontWeight: 600 }}>
                    {result.model || selectedModel}
                  </strong>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                  <span style={{ fontSize: '1rem' }}>🎬</span>
                  <span>Generated Clips:</span>
                  <strong style={{ color: 'var(--text-primary)', fontWeight: 600 }}>
                    {result.clips.length}
                  </strong>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                  <span style={{ fontSize: '1rem' }}>🔖</span>
                  <span>Marked Clips:</span>
                  <strong style={{ color: 'var(--secondary)', fontWeight: 600 }}>
                    {result.clips.filter(clip => !!markedClips[`${clip.start_time}_${clip.end_time}`]).length}
                  </strong>
                </div>
              </div>

              {/* Search & Filter Controls */}
              <div className="filter-controls-bar">
                <input
                  type="text"
                  className="form-input search-filter-input"
                  placeholder="🔍 Search clips or transcripts..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  style={{ padding: '0.6rem 1rem', fontSize: '0.875rem' }}
                />

                <select
                  className="form-input virality-filter-select"
                  value={viralityFilter}
                  onChange={(e) => setViralityFilter(e.target.value as any)}
                  style={{ width: 'auto', padding: '0.6rem 2rem 0.6rem 1rem', fontSize: '0.875rem', cursor: 'pointer' }}
                >
                  <option value="all">🔥 All Scores</option>
                  <option value="high">🚀 High (90%+)</option>
                  <option value="medium">📈 Mid/Low (&lt;90%)</option>
                  <option value="marked">🔖 Marked Only</option>
                </select>

                <select
                  className="form-input virality-filter-select"
                  value={sortBy}
                  onChange={(e) => setSortBy(e.target.value as any)}
                  style={{ width: 'auto', padding: '0.6rem 2rem 0.6rem 1rem', fontSize: '0.875rem', cursor: 'pointer' }}
                >
                  <option value="virality">🔥 Sort: Virality</option>
                  <option value="time">⏰ Sort: Chronological</option>
                  <option value="duration">⏱️ Sort: Duration</option>
                  <option value="marked">🔖 Sort: Marked</option>
                </select>
              </div>

              {/* Stats and Exports */}
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: '0.8rem', color: 'var(--text-secondary)', marginTop: '0.25rem' }}>
                <div>
                  Showing {sortedClips.length} of {result.clips.length} clips
                </div>
                <div style={{ display: 'flex', gap: '0.75rem' }}>
                  <button
                    type="button"
                    className="action-link-btn"
                    onClick={handleCopyAllMarkdown}
                    style={{ background: 'none', border: 'none', color: 'var(--primary)', cursor: 'pointer', fontWeight: 600, fontSize: '0.8rem' }}
                  >
                    📋 Copy All (MD)
                  </button>
                  <span style={{ color: 'var(--border-color)' }}>|</span>
                  <button
                    type="button"
                    className="action-link-btn"
                    onClick={handleExportJSON}
                    style={{ background: 'none', border: 'none', color: 'var(--secondary)', cursor: 'pointer', fontWeight: 600, fontSize: '0.8rem' }}
                  >
                    📥 Download JSON
                  </button>
                  {result.transcript && (
                    <>
                      <span style={{ color: 'var(--border-color)' }}>|</span>
                      <button
                        type="button"
                        className="action-link-btn"
                        onClick={handleExportSRT}
                        style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', fontWeight: 600, fontSize: '0.8rem' }}
                      >
                        📥 Download SRT
                      </button>
                    </>
                  )}
                </div>
              </div>
            </div>

            <div className="clips-list" style={{ maxHeight: 'none', flex: 1 }}>
              {sortedClips.length === 0 ? (
                <div style={{ textAlign: 'center', padding: '3rem 1rem', color: 'var(--text-muted)', fontSize: '0.9rem' }}>
                  No clips match the active filters. Try refining your search.
                </div>
              ) : (
                sortedClips.map((clip, index) => {
                  const isActive = activeClip?.start_time === clip.start_time && activeClip?.end_time === clip.end_time;
                  const isExpanded = expandedClipIndex === index;

                  return (
                    <div
                      key={index}
                      id={`clip-card-${index}`}
                      className={`clip-card ${isActive ? 'active' : ''}`}
                      onClick={() => {
                        setActiveClip(clip);
                        setExpandedClipIndex(isExpanded ? null : index);
                      }}
                    >
                      {/* Header */}
                      <div className="clip-header">
                        <div style={{ display: 'flex', alignItems: 'center', marginTop: '0.25rem' }}>
                          <input
                            type="checkbox"
                            checked={!!markedClips[`${clip.start_time}_${clip.end_time}`]}
                            onChange={(e) => {
                              e.stopPropagation();
                              toggleMarkedClip(`${clip.start_time}_${clip.end_time}`);
                            }}
                            style={{
                              width: '18px',
                              height: '18px',
                              cursor: 'pointer',
                              accentColor: 'var(--primary)'
                            }}
                          />
                        </div>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem', flex: 1 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
                            <span className="clip-title" style={{ color: !!markedClips[`${clip.start_time}_${clip.end_time}`] ? 'var(--secondary)' : 'var(--text-primary)', opacity: 1 }}>
                              {clip.title}
                            </span>
                            <button
                              type="button"
                              onClick={(e) => {
                                e.stopPropagation();
                                handleCopyText(clip.title, 'Title');
                              }}
                              style={{
                                background: 'rgba(255, 255, 255, 0.05)',
                                border: '1px solid var(--border-color)',
                                cursor: 'pointer',
                                padding: '2px 6px',
                                borderRadius: '4px',
                                fontSize: '0.7rem',
                                display: 'inline-flex',
                                alignItems: 'center',
                                gap: '2px',
                                color: 'var(--text-secondary)',
                                transition: 'var(--transition-smooth)'
                              }}
                              onMouseEnter={(e) => {
                                e.currentTarget.style.background = 'rgba(168, 85, 247, 0.15)';
                                e.currentTarget.style.color = 'var(--primary)';
                                e.currentTarget.style.borderColor = 'rgba(168, 85, 247, 0.3)';
                              }}
                              onMouseLeave={(e) => {
                                e.currentTarget.style.background = 'rgba(255, 255, 255, 0.05)';
                                e.currentTarget.style.color = 'var(--text-secondary)';
                                e.currentTarget.style.borderColor = 'var(--border-color)';
                              }}
                              title="Copy Title to clipboard"
                            >
                              📋 Copy
                            </button>
                          </div>
                          <div className="score-meta" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
                            <span className="timestamp-pill">
                              {formatSeconds(clip.start_time)} - {formatSeconds(clip.end_time)}
                            </span>
                            <span>Duration: {formatSeconds(clip.end_time - clip.start_time)}</span>
                            {clip.hook_time !== undefined && (
                              <span 
                                onClick={(e) => {
                                  e.stopPropagation();
                                  handleSeek(clip.hook_time!);
                                }}
                                style={{
                                  display: 'inline-flex',
                                  alignItems: 'center',
                                  gap: '2px',
                                  fontSize: '0.72rem',
                                  fontWeight: 'bold',
                                  color: 'var(--accent)',
                                  background: 'rgba(16, 185, 129, 0.12)',
                                  border: '1px solid rgba(16, 185, 129, 0.35)',
                                  borderRadius: '4px',
                                  padding: '0.05rem 0.35rem',
                                  cursor: 'pointer',
                                  whiteSpace: 'nowrap'
                                }}
                                title="Click to jump to the potential hook timestamp"
                              >
                                🪝 Hook: {formatSeconds(clip.hook_time)}
                              </span>
                            )}
                          </div>
                        </div>
                        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '0.35rem' }}>
                          <div className={`score-badge ${clip.virality_score >= 90 ? 'score-high' : 'score-medium'}`}>
                            <span>🔥</span>
                            <span>{clip.virality_score}% Virality</span>
                          </div>
                          {!!markedClips[`${clip.start_time}_${clip.end_time}`] && (
                            <div style={{
                              display: 'flex',
                              alignItems: 'center',
                              gap: '4px',
                              fontSize: '0.65rem',
                              fontWeight: 'bold',
                              color: 'var(--secondary)',
                              background: 'rgba(255, 94, 58, 0.12)',
                              padding: '0.15rem 0.4rem',
                              borderRadius: '4px',
                              border: '1px solid var(--secondary)'
                            }}>
                              ✓ MARKED
                            </div>
                          )}
                        </div>
                      </div>

                      {/* Key spoken quotes */}
                      {clip.key_quotes && clip.key_quotes.length > 0 && (
                        <div className="clip-quotes">
                          {clip.key_quotes.map((quote, qIdx) => (
                            <div key={qIdx} className="quote-item">“{quote}”</div>
                          ))}
                        </div>
                      )}

                      {/* Suggestions: Title, Caption */}
                      {(clip.title_suggestion || clip.caption_suggestion) && (
                        <div className="clip-suggestions">
                          {clip.title_suggestion && (
                            <div className="suggestion-item" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                              <div style={{ flex: 1 }}>
                                <span className="suggestion-label">💡 Title:</span>{' '}
                                <span className="suggestion-value">{clip.title_suggestion}</span>
                              </div>
                              <button
                                type="button"
                                className="copy-mini-btn"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  handleCopyText(clip.title_suggestion!, 'Title');
                                }}
                                title="Copy Title"
                              >
                                📋
                              </button>
                            </div>
                          )}
                          {clip.caption_suggestion && (
                            <div className="suggestion-item" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                              <div style={{ flex: 1 }}>
                                <span className="suggestion-label">📝 Caption:</span>{' '}
                                <span className="suggestion-value">
                                  {(() => {
                                    const lowercaseHashtags = (clip.hashtag_suggestion || '').toLowerCase();
                                    if (!lowercaseHashtags) return clip.caption_suggestion;
                                    if (clip.caption_suggestion.toLowerCase().includes(lowercaseHashtags)) return clip.caption_suggestion;
                                    return `${clip.caption_suggestion} ${lowercaseHashtags}`;
                                  })()}
                                </span>
                              </div>
                              <button
                                type="button"
                                className="copy-mini-btn"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  const captionText = (() => {
                                    const caption = clip.caption_suggestion || '';
                                    const lowercaseHashtags = (clip.hashtag_suggestion || '').toLowerCase();
                                    if (!lowercaseHashtags) return caption;
                                    if (caption.toLowerCase().includes(lowercaseHashtags)) return caption;
                                    return `${caption} ${lowercaseHashtags}`;
                                  })();
                                  handleCopyText(captionText, 'Caption');
                                }}
                                title="Copy Caption"
                              >
                                📋
                              </button>
                            </div>
                          )}
                        </div>
                      )}

                      {/* Actions and expand button */}
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '0.5rem', borderTop: '1px solid rgba(255,255,255,0.03)', paddingTop: '0.75rem' }}>
                        <button
                          type="button"
                          className="glowing-btn"
                          style={{ padding: '0.45rem 1rem', fontSize: '0.8rem', borderRadius: '8px', boxShadow: 'none' }}
                          onClick={(e) => {
                            e.stopPropagation();
                            playClip(clip);
                          }}
                        >
                          ⚡ Preview Clip
                        </button>

                        <div style={{ display: 'flex', gap: '0.75rem' }}>
                          <button
                            type="button"
                            className="form-input"
                            style={{ padding: '0.45rem 1rem', fontSize: '0.8rem', width: 'auto', borderRadius: '8px', cursor: 'pointer', background: 'transparent' }}
                            onClick={(e) => handleCopyClip(clip, e)}
                          >
                            📋 Copy Timestamp
                          </button>
                          <span
                            style={{ display: 'flex', alignItems: 'center', fontSize: '0.75rem', color: 'var(--primary)', fontWeight: 'bold' }}
                          >
                            {isExpanded ? 'Hide Transcript ▲' : 'Show Transcript ▼'}
                          </span>
                        </div>
                      </div>

                      {/* Expandable transcript text block */}
                      {isExpanded && (
                        <div
                          className="transcript-box"
                          onClick={(e) => e.stopPropagation()} /* Prevents collapse */
                        >
                          <div style={{ fontSize: '0.75rem', fontWeight: 'bold', color: 'var(--primary)', marginBottom: '0.25rem', textTransform: 'uppercase' }}>Transcript</div>
                          {clip.transcript}
                        </div>
                      )}
                    </div>
                  );
                })
              )}
            </div>
          </div>
        </main>
      )}

      {/* Global CSS spinner keyframe animation injection */}
      <style>{`
        @keyframes spin {
          0% { transform: rotate(0deg); }
          100% { transform: rotate(360deg); }
        }
        @keyframes pulse {
          0% { opacity: 0.6; }
          50% { opacity: 1; }
          100% { opacity: 0.6; }
        }
        .spinner-icon {
          animation: spin 1s linear infinite;
        }
        .pulsing-text {
          animation: pulse 2s infinite ease-in-out;
        }
        .nav-link:hover {
          color: var(--primary) !important;
        }
      `}</style>
    </div>
  );
}
