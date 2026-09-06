import React from 'react';
import { useLanguage } from '../locales';

export const LanguageSwitcher: React.FC = () => {
  const { language, setLanguage } = useLanguage();

  return (
    <div
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        padding: '3px',
        borderRadius: '20px',
        background: 'rgba(255, 255, 255, 0.04)',
        border: '1px solid var(--border-color)',
        backdropFilter: 'blur(8px)',
        position: 'relative',
        userSelect: 'none',
      }}
      role="group"
      aria-label="Language selection"
    >
      <button
        type="button"
        onClick={() => setLanguage('id')}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: '4px',
          padding: '4px 10px',
          fontSize: '0.75rem',
          fontWeight: 600,
          border: 'none',
          borderRadius: '16px',
          cursor: 'pointer',
          transition: 'all 0.2s ease',
          background: language === 'id' 
            ? 'linear-gradient(135deg, var(--primary) 0%, #a855f7 100%)' 
            : 'transparent',
          color: language === 'id' ? '#ffffff' : 'var(--text-secondary)',
          boxShadow: language === 'id' ? '0 2px 8px rgba(168, 85, 247, 0.35)' : 'none',
        }}
        title="Bahasa Indonesia"
      >
        <span>🇮🇩</span>
        <span>ID</span>
      </button>

      <button
        type="button"
        onClick={() => setLanguage('en')}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: '4px',
          padding: '4px 10px',
          fontSize: '0.75rem',
          fontWeight: 600,
          border: 'none',
          borderRadius: '16px',
          cursor: 'pointer',
          transition: 'all 0.2s ease',
          background: language === 'en' 
            ? 'linear-gradient(135deg, var(--primary) 0%, #a855f7 100%)' 
            : 'transparent',
          color: language === 'en' ? '#ffffff' : 'var(--text-secondary)',
          boxShadow: language === 'en' ? '0 2px 8px rgba(168, 85, 247, 0.35)' : 'none',
        }}
        title="English"
      >
        <span>🇬🇧</span>
        <span>EN</span>
      </button>
    </div>
  );
};
