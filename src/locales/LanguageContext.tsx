import React, { createContext, useContext, useState, useEffect, useMemo } from 'react';
import { en, type Translations } from './en';
import { id } from './id';

export type Language = 'en' | 'id';

interface LanguageContextType {
  language: Language;
  setLanguage: (lang: Language) => void;
  t: Translations;
}

const STORAGE_KEY = 'cheat_clip_language';

const getInitialLanguage = (): Language => {
  if (typeof window === 'undefined') return 'en';
  
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved === 'en' || saved === 'id') {
    return saved;
  }

  // Detect device / browser language
  const browserLang = (navigator.language || (navigator as any).userLanguage || '').toLowerCase();
  if (browserLang.startsWith('id')) {
    return 'id';
  }
  return 'en';
};

const LanguageContext = createContext<LanguageContextType | undefined>(undefined);

export const LanguageProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [language, setLanguageState] = useState<Language>(getInitialLanguage);

  const setLanguage = (newLang: Language) => {
    setLanguageState(newLang);
    localStorage.setItem(STORAGE_KEY, newLang);
  };

  // Sync state if another tab updates the preference
  useEffect(() => {
    const handleStorageChange = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY && (e.newValue === 'en' || e.newValue === 'id')) {
        setLanguageState(e.newValue);
      }
    };
    window.addEventListener('storage', handleStorageChange);
    return () => window.removeEventListener('storage', handleStorageChange);
  }, []);

  const t = useMemo(() => (language === 'id' ? id : en), [language]);

  return (
    <LanguageContext.Provider value={{ language, setLanguage, t }}>
      {children}
    </LanguageContext.Provider>
  );
};

export const useLanguage = (): LanguageContextType => {
  const context = useContext(LanguageContext);
  if (!context) {
    throw new Error('useLanguage must be used within a LanguageProvider');
  }
  return context;
};
