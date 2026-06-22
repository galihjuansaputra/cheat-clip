import React, { useRef, useState, useEffect } from 'react';
import type { HeatmapPoint } from '../types';

interface HeatmapTimelineProps {
  duration: number;
  heatmap: HeatmapPoint[];
  currentTime: number;
  onSeek: (seconds: number) => void;
  activeClip?: { start_time: number; end_time: number } | null;
}

export const HeatmapTimeline: React.FC<HeatmapTimelineProps> = ({
  duration,
  heatmap,
  currentTime,
  onSeek,
  activeClip,
}) => {
  const svgRef = useRef<SVGSVGElement>(null);
  const [hoverX, setHoverX] = useState<number | null>(null);
  const [hoverTime, setHoverTime] = useState<number>(0);
  const [hoverValue, setHoverValue] = useState<number>(0);

  const height = 70;
  const paddingBottom = 5;
  const paddingTop = 15;
  const chartHeight = height - paddingBottom - paddingTop;

  // Format seconds to MM:SS
  const formatTime = (secs: number) => {
    const m = Math.floor(secs / 60);
    const s = Math.floor(secs % 60);
    return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  };

  // Convert points to SVG coordinates
  const getCoordinates = (svgWidth: number) => {
    if (!heatmap || heatmap.length === 0 || duration === 0) return [];
    
    return heatmap.map((point, index) => {
      // Position x based on time or index (index is safer for uniform distribution)
      const x = (index / (heatmap.length - 1)) * svgWidth;
      // Invert Y coordinate so 1.0 value is at top
      const y = paddingTop + chartHeight - (point.value * chartHeight);
      return { x, y, value: point.value, time: point.start_time };
    });
  };

  const handleMouseMove = (e: React.MouseEvent<SVGSVGElement>) => {
    if (!svgRef.current || duration === 0) return;
    
    const rect = svgRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const pct = Math.max(0, Math.min(1, x / rect.width));
    const time = pct * duration;
    
    // Find closest heatmap value
    let val = 0;
    if (heatmap && heatmap.length > 0) {
      const closestPoint = heatmap.reduce((prev, curr) => {
        const prevDiff = Math.abs((prev.start_time + prev.end_time) / 2 - time);
        const currDiff = Math.abs((curr.start_time + curr.end_time) / 2 - time);
        return prevDiff < currDiff ? prev : curr;
      });
      val = closestPoint.value;
    }
    
    setHoverX(x);
    setHoverTime(time);
    setHoverValue(val);
  };

  const handleMouseLeave = () => {
    setHoverX(null);
  };

  const handleClick = (e: React.MouseEvent<SVGSVGElement>) => {
    if (!svgRef.current || duration === 0) return;
    const rect = svgRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const pct = Math.max(0, Math.min(1, x / rect.width));
    onSeek(pct * duration);
  };

  const [svgWidth, setSvgWidth] = useState(600);

  // Resize handler
  useEffect(() => {
    if (!svgRef.current) return;
    const updateWidth = () => {
      if (svgRef.current) {
        setSvgWidth(svgRef.current.clientWidth);
      }
    };
    
    updateWidth();
    window.addEventListener('resize', updateWidth);
    
    // Set a timeout to trigger another resize check after the layout settles
    const timer = setTimeout(updateWidth, 100);
    
    return () => {
      window.removeEventListener('resize', updateWidth);
      clearTimeout(timer);
    };
  }, [heatmap]);

  const coords = getCoordinates(svgWidth);
  
  // Build SVG Path lines
  let strokePath = '';
  let fillPath = '';

  if (coords.length > 0) {
    const pointsStr = coords.map(c => `${c.x},${c.y}`).join(' ');
    strokePath = `M ${pointsStr}`;
    fillPath = `M 0,${height} L ${pointsStr} L ${svgWidth},${height} Z`;
  }

  // Playback cursor positioning
  const playheadX = duration > 0 ? (currentTime / duration) * svgWidth : 0;

  // Active clip highlight boundaries
  let activeClipStart = 0;
  let activeClipWidth = 0;
  if (activeClip && duration > 0) {
    activeClipStart = (activeClip.start_time / duration) * svgWidth;
    const activeClipEnd = (activeClip.end_time / duration) * svgWidth;
    activeClipWidth = Math.max(2, activeClipEnd - activeClipStart);
  }

  return (
    <div style={{ width: '100%', display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.75rem', color: 'var(--text-muted)', fontWeight: 'bold' }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
          Audience Retention Heatmap
          {(!heatmap || heatmap.length === 0) && (
            <span style={{
              fontSize: '0.62rem',
              fontWeight: 500,
              color: 'rgba(255,255,255,0.3)',
              background: 'rgba(255,255,255,0.06)',
              padding: '0.1rem 0.4rem',
              borderRadius: '4px',
              border: '1px solid rgba(255,255,255,0.1)'
            }}>Not available for this video</span>
          )}
        </span>
        <span>{formatTime(currentTime)} / {formatTime(duration)}</span>
      </div>
      
      <div style={{ position: 'relative', background: 'rgba(5, 7, 15, 0.6)', borderRadius: '8px', border: '1px solid var(--border-color)', padding: '2px', overflow: 'hidden' }}>
        <svg
          ref={svgRef}
          width="100%"
          height={height}
          style={{ cursor: 'pointer', display: 'block' }}
          onMouseMove={handleMouseMove}
          onMouseLeave={handleMouseLeave}
          onClick={handleClick}
        >
          <defs>
            {/* Background Heatmap Gradient */}
            <linearGradient id="heatmapFillGradient" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--primary)" stopOpacity="0.45" />
              <stop offset="100%" stopColor="var(--primary)" stopOpacity="0.0" />
            </linearGradient>
            
            {/* Active Range Pattern */}
            <linearGradient id="activeRangeGrad" x1="0" y1="0" x2="1" y2="0">
              <stop offset="0%" stopColor="var(--secondary)" stopOpacity="0.25" />
              <stop offset="100%" stopColor="var(--secondary)" stopOpacity="0.05" />
            </linearGradient>
          </defs>

          {/* Fallback dotted baseline when no heatmap exists */}
          {(!heatmap || heatmap.length === 0) && (
            <line
              x1="0"
              y1={height - 20}
              x2={svgWidth}
              y2={height - 20}
              stroke="var(--border-color)"
              strokeDasharray="4,4"
              strokeWidth="2"
            />
          )}

          {/* Render Heatmap Curve */}
          {coords.length > 0 && (
            <>
              {/* Heatmap Area Fill */}
              <path d={fillPath} fill="url(#heatmapFillGradient)" />
              {/* Heatmap Stroke Line */}
              <path d={strokePath} fill="none" stroke="var(--primary)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
            </>
          )}

          {/* Active Clip Highlight Zone */}
          {activeClip && activeClipWidth > 0 && (
            <g>
              <rect
                x={activeClipStart}
                y={paddingTop - 5}
                width={activeClipWidth}
                height={chartHeight + 10}
                fill="url(#activeRangeGrad)"
                stroke="var(--secondary)"
                strokeWidth="1.5"
                strokeDasharray="3,3"
                rx="4"
              />
            </g>
          )}

          {/* Live Playback cursor line */}
          {duration > 0 && (
            <g>
              <line
                x1={playheadX}
                y1="0"
                x2={playheadX}
                y2={height}
                stroke="var(--text-primary)"
                strokeWidth="2"
                style={{ filter: 'drop-shadow(0 0 4px rgba(255,255,255,0.6))' }}
              />
              <circle
                cx={playheadX}
                cy={height / 2}
                r="4"
                fill="var(--text-primary)"
              />
            </g>
          )}

          {/* Hover Guide Line & Tooltip Indicator */}
          {hoverX !== null && (
            <g>
              <line
                x1={hoverX}
                y1="0"
                x2={hoverX}
                y2={height}
                stroke="rgba(255, 255, 255, 0.25)"
                strokeWidth="1"
                strokeDasharray="2,2"
              />
              <circle
                cx={hoverX}
                cy={coords.length > 0 ? coords[Math.floor((hoverX / svgWidth) * (coords.length - 1))]?.y || height / 2 : height / 2}
                r="5"
                fill="var(--secondary)"
                style={{ filter: 'drop-shadow(0 0 3px var(--secondary-glow))' }}
              />
            </g>
          )}
        </svg>

        {/* Hover Tooltip HTML positioning */}
        {hoverX !== null && (
          <div
            style={{
              position: 'absolute',
              top: '4px',
              left: `${Math.min(svgWidth - 90, Math.max(10, hoverX - 45))}px`,
              background: 'rgba(15, 23, 42, 0.95)',
              border: '1px solid rgba(255, 255, 255, 0.1)',
              borderRadius: '4px',
              padding: '2px 6px',
              fontSize: '0.65rem',
              color: 'var(--text-primary)',
              pointerEvents: 'none',
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              boxShadow: '0 4px 6px rgba(0, 0, 0, 0.3)',
              zIndex: 10,
            }}
          >
            <span style={{ fontWeight: 'bold' }}>{formatTime(hoverTime)}</span>
            {heatmap && heatmap.length > 0 && (
              <span style={{ color: 'var(--secondary)', fontSize: '0.6rem' }}>
                Interest: {Math.round(hoverValue * 100)}%
              </span>
            )}
          </div>
        )}
      </div>
      
      {/* Timeline legends */}
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.65rem', color: 'var(--text-muted)' }}>
        <span>00:00</span>
        {heatmap && heatmap.length > 0 && (
          <span style={{ display: 'flex', gap: '0.75rem', alignItems: 'center' }}>
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: '3px' }}>
              <span style={{ width: '8px', height: '8px', borderRadius: '50%', background: 'var(--primary)' }}></span>
              Most Replayed peaks
            </span>
            {activeClip && (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: '3px' }}>
                <span style={{ width: '8px', height: '8px', borderRadius: '2px', border: '1px dashed var(--secondary)', background: 'rgba(255, 94, 58, 0.1)' }}></span>
                Active Clip Zone
              </span>
            )}
          </span>
        )}
        <span>{formatTime(duration)}</span>
      </div>
    </div>
  );
};
export default HeatmapTimeline;
