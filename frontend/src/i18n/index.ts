import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import zh from "./zh.json";
import en from "./en.json";

i18n
    .use(LanguageDetector)
    .use(initReactI18next)
    .init({
        resources: {
            zh: { translation: zh },
            en: { translation: en },
        },
        fallbackLng: "en",
        lng: "en",  // default for first-time visitors; localStorage override wins on subsequent loads
        interpolation: { escapeValue: false },
        supportedLngs: ["en", "zh"],
        nonExplicitSupportedLngs: true,
        detection: {
            order: ["localStorage", "navigator"],
            caches: ["localStorage"],
            convertDetectedLanguage: (lng) => {
                // Only honour an explicit Chinese preference; everything else
                // falls back to English (prevents ko-KR / ja-JP / es-MX etc.
                // users landing on the Chinese locale).
                if (lng.startsWith("zh")) return "zh";
                return "en";
            },
        },
        react: {
            // Disables the Suspense wrapper that races with React reconciliation
            // on i18n.changeLanguage() — classic NotFoundError: Failed to execute
            // 'removeChild' on 'Node' when toggling between en <-> zh.
            useSuspense: false,
        },
    });

export default i18n;
