import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import type { NewsItem } from "../../../domain/news/NewsItem";
import { NewsSentiment } from "../../../domain/news/NewsSentiment";
import { newsService } from "../../../application/news/newsServiceInstance";
import { PixelPanel } from "../layout/PixelPanel";

type NewsPanelProps = {
  turnIndex: number;
  selectedAssetId?: string | null;
};

export function NewsPanel({ turnIndex, selectedAssetId }: NewsPanelProps) {
  const [items, setItems] = useState<NewsItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;

    const load = async () => {
      setLoading(true);
      setError(null);

      try {
        const newsItems = await newsService.getTurnNews({
          turnIndex,
          selectedAssetId: selectedAssetId ?? null,
          limit: 6,
        });

        if (alive) {
          setItems(newsItems);
        }
      } catch (e) {
        if (alive) {
          setError(String(e));
          setItems([]);
        }
      } finally {
        if (alive) {
          setLoading(false);
        }
      }
    };

    void load();

    return () => {
      alive = false;
    };
  }, [turnIndex, selectedAssetId]);

  return (
    <PixelPanel title="뉴스">
      <div style={headerStyle}>
        <span>Turn {turnIndex}</span>
        {selectedAssetId ? (
          <span>선택 종목: {selectedAssetId}</span>
        ) : (
          <span>전체 뉴스</span>
        )}
      </div>

      {loading && <p style={mutedStyle}>뉴스 불러오는 중...</p>}

      {error && <p style={errorStyle}>{error}</p>}

      {!loading && !error && items.length === 0 && (
        <div style={emptyStyle}>
          <strong>뉴스 없음</strong>
          <p>
            현재 턴에 연결된 뉴스가 없습니다. Supabase의 news.turn_index와
            game_calendar.turn_index 매핑을 확인하세요.
          </p>
        </div>
      )}

      {!loading &&
        !error &&
        items.map((item) => <NewsCard key={item.id} item={item} />)}
    </PixelPanel>
  );
}

function NewsCard({ item }: { item: NewsItem }) {
  return (
    <article style={cardStyle}>
      <div style={cardTopStyle}>
        <span style={badgeStyle}>{formatAssetLabel(item)}</span>
        <span style={sentimentStyle(item.sentiment)}>
          {formatSentiment(item.sentiment)}
        </span>
      </div>

      <h3 style={headlineStyle}>{item.headline}</h3>
      <p style={bodyStyle}>{item.body}</p>

      <div style={metaStyle}>
        <span>{item.gameDate}</span>
        {item.newsOrder !== null && <span>#{item.newsOrder}</span>}
      </div>
    </article>
  );
}

function formatAssetLabel(item: NewsItem): string {
  if (item.assetId) {
    return item.assetId;
  }

  if (item.assetClass) {
    return item.assetClass.toUpperCase();
  }

  return "GLOBAL";
}

function formatSentiment(sentiment: NewsSentiment): string {
  if (sentiment === NewsSentiment.POSITIVE) {
    return "긍정";
  }

  if (sentiment === NewsSentiment.NEGATIVE) {
    return "부정";
  }

  return "중립";
}

function sentimentStyle(sentiment: NewsSentiment): CSSProperties {
  const base: CSSProperties = {
    padding: "4px 8px",
    border: "1px solid #555",
    fontSize: "12px",
    fontWeight: 800,
  };

  if (sentiment === NewsSentiment.POSITIVE) {
    return {
      ...base,
      color: "#ff7675",
    };
  }

  if (sentiment === NewsSentiment.NEGATIVE) {
    return {
      ...base,
      color: "#74b9ff",
    };
  }

  return {
    ...base,
    color: "#dcdcdc",
  };
}

const headerStyle: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  gap: "12px",
  marginBottom: "12px",
  color: "#bdbdbd",
  fontSize: "13px",
};

const mutedStyle: CSSProperties = {
  color: "#bdbdbd",
};

const errorStyle: CSSProperties = {
  color: "#ff6b6b",
};

const emptyStyle: CSSProperties = {
  border: "1px solid #555",
  background: "#1d1d1d",
  padding: "12px",
  color: "#dcdcdc",
};

const cardStyle: CSSProperties = {
  border: "1px solid #555",
  background: "#1d1d1d",
  padding: "12px",
  marginBottom: "10px",
};

const cardTopStyle: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  gap: "8px",
  marginBottom: "8px",
};

const badgeStyle: CSSProperties = {
  padding: "4px 8px",
  border: "1px solid #f7e72f",
  color: "#f7e72f",
  fontSize: "12px",
  fontWeight: 800,
};

const headlineStyle: CSSProperties = {
  margin: "0 0 8px",
  fontSize: "16px",
  lineHeight: 1.4,
};

const bodyStyle: CSSProperties = {
  margin: 0,
  color: "#dcdcdc",
  lineHeight: 1.5,
};

const metaStyle: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  marginTop: "10px",
  color: "#999",
  fontSize: "12px",
};