"""the interactive cli for searching, downloading, filtering by year, rate, language and etc. and other features"""
import sqlite3
import os
import sys
import requests
from urllib.parse import urlparse
from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, DownloadColumn, TransferSpeedColumn
from rapidfuzz import process, fuzz

DB_NAME = "f2mc_database.db"
console = Console()

class MediaCLI:
    def __init__(self):
        if not os.path.exists(DB_NAME):
            console.print(f"[bold red]Database '{DB_NAME}' not found. Please run the scraper first.[/bold red]")
            sys.exit(1)
            
        self.conn = sqlite3.connect(DB_NAME, check_same_thread=False)
        self.default_download_dir = os.path.expanduser("~/Downloads/f2mc_media")
        
        os.makedirs(self.default_download_dir, exist_ok=True)

    def get_genres(self):
        c = self.conn.cursor()
        c.execute("SELECT DISTINCT genres FROM media WHERE genres IS NOT NULL AND genres != ''")
        all_genres = set()
        for row in c.fetchall():
            for g in row[0].split(','):
                g = g.strip()
                if g:
                    all_genres.add(g)
        return sorted(list(all_genres))

    def get_countries(self):
        c = self.conn.cursor()
        c.execute("SELECT DISTINCT country FROM media WHERE country IS NOT NULL AND country != 'Unknown'")
        countries = set()
        for row in c.fetchall():
            for c_name in row[0].split(','):
                c_name = c_name.strip()
                if c_name:
                    countries.add(c_name)
        return sorted(list(countries))

    def get_languages(self):
        c = self.conn.cursor()
        c.execute("SELECT DISTINCT language FROM media WHERE language IS NOT NULL AND language != 'Unknown'")
        languages = set()
        for row in c.fetchall():
            for lang in row[0].split(','):
                lang = lang.strip()
                if lang:
                    languages.add(lang)
        return sorted(list(languages))

    def fuzzy_search_media(self, query, media_type="Both"):
        c = self.conn.cursor()
        if media_type == "Both":
            c.execute("SELECT id, title, type, imdb_rate, published_year, actors FROM media")
        else:
            c.execute("SELECT id, title, type, imdb_rate, published_year, actors FROM media WHERE type=?", (media_type,))
            
        rows = c.fetchall()
        titles = [row[1] for row in rows]
        
        results = process.extract(query, titles, scorer=fuzz.WRatio, limit=20)
        
        matched_media = []
        for title, score, idx in results:
            if score > 50:
                row = rows[idx]
                matched_media.append({
                    "id": row[0],
                    "title": row[1],
                    "type": row[2],
                    "imdb": row[3],
                    "year": row[4] if row[4] else "N/A",
                    "actors": row[5] if row[5] else "N/A"
                })
        return matched_media

    def filter_media(self, genres=None, countries=None, media_type="Both", actors=None, directors=None, min_rating=0, min_year=0, max_year=9999, languages=None, time_filter=None):
        c = self.conn.cursor()
        query = "SELECT id, title, type, imdb_rate, published_year, actors FROM media WHERE 1=1"
        params = []
        
        if media_type != "Both":
            query += " AND type=?"
            params.append(media_type)
            
        if genres:
            like_clauses = " OR ".join(["genres LIKE ?" for _ in genres])
            query += f" AND ({like_clauses})"
            params.extend([f"%{g}%" for g in genres])
            
        if countries:
            like_clauses = " OR ".join(["country LIKE ?" for _ in countries])
            query += f" AND ({like_clauses})"
            params.extend([f"%{c}%" for c in countries])

        if languages:
            like_clauses = " OR ".join(["language LIKE ?" for _ in languages])
            query += f" AND ({like_clauses})"
            params.extend([f"%{l}%" for l in languages])
            
        if actors:
            like_clauses = " OR ".join(["actors LIKE ?" for _ in actors])
            query += f" AND ({like_clauses})"
            params.extend([f"%{a}%" for a in actors])

        if directors:
            like_clauses = " OR ".join(["directors LIKE ?" for _ in directors])
            query += f" AND ({like_clauses})"
            params.extend([f"%{d}%" for d in directors])

        if min_rating > 0:
            query += " AND CAST(imdb_rate AS REAL) >= ?"
            params.append(min_rating)

        if min_year > 0:
            query += " AND published_year >= ?"
            params.append(min_year)

        if max_year < 9999:
            query += " AND published_year <= ?"
            params.append(max_year)

        if time_filter:
            if time_filter == "Under 30 mins":
                query += " AND avg_time > 0 AND avg_time < 30"
            elif time_filter == "30 - 60 mins":
                query += " AND avg_time >= 30 AND avg_time <= 60"
            elif time_filter == "Over 60 mins":
                query += " AND avg_time > 60"
            
        c.execute(query, params)
        return [{"id": row[0], "title": row[1], "type": row[2], "imdb": row[3], "year": row[4] if row[4] else "N/A", "actors": row[5] if row[5] else "N/A"} for row in c.fetchall()]

    def get_links(self, media_id):
        c = self.conn.cursor()
        c.execute('''
            SELECT id, season, episode, quality, sub_status, url 
            FROM links WHERE media_id=? 
            ORDER BY season ASC, CAST(episode AS INTEGER) ASC
        ''', (media_id,))
        return c.fetchall()

    def download_file(self, url, dest_dir):
        try:
            parsed_url = urlparse(url)
            filename = os.path.basename(parsed_url.path)
            if not filename:
                filename = "downloaded_media.mkv"
                
            filepath = os.path.join(dest_dir, filename)
            
            if os.path.exists(filepath):
                if not inquirer.confirm(message=f"File '{filename}' already exists. Overwrite?", default=False).execute():
                    return

            console.print(f"[cyan]Starting download:[/cyan] {filename}")
            
            with requests.get(url, stream=True, timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as r:
                r.raise_for_status()
                total_size = int(r.headers.get('content-length', 0))
                
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    DownloadColumn(),
                    TransferSpeedColumn(),
                    console=console,
                ) as progress:
                    task = progress.add_task("Downloading", total=total_size)
                    with open(filepath, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                            progress.update(task, advance=len(chunk))
                            
            console.print(f"[bold green]✓ Download complete:[/bold green] {filepath}\n")
            
        except Exception as e:
            console.print(f"[bold red]Error downloading {url}:[/bold red] {e}")

    def display_media_table(self, media_list):
        table = Table(title="Search Results", show_lines=True)
        table.add_column("ID", style="dim")
        table.add_column("Title", style="bold cyan")
        table.add_column("Type")
        table.add_column("Year", justify="center")
        table.add_column("IMDB", justify="right")
        table.add_column("Actors", style="dim", overflow="fold")
        
        for m in media_list:
            actors_str = str(m.get('actors', 'N/A'))
            if len(actors_str) > 50:
                actors_str = actors_str[:50] + "..."
                
            table.add_row(
                str(m['id']), 
                m['title'], 
                m['type'], 
                str(m.get('year', 'N/A')), 
                str(m['imdb']),
                actors_str
            )
        console.print(table)

    def main_loop(self):
        while True:
            console.print("\n[bold blue]===== Cinema Terminal =====[/bold blue]")

            media_type = inquirer.select(
                message="Select media type:",
                choices=["Movies", "Series", "Both"],
                default="Both",
            ).execute()

            if media_type == 'Movies':
                media_type = 'Movie'

            action = inquirer.select(
                message="How do you want to search?",
                choices=[
                    "Search by Name (Fuzzy)",
                    "Filter by Genres & Countries",
                    "Search by Actor",
                    "Search by Director",
                    "Advanced Filters (Year, Rate, Time, Language)",
                    "Exit"
                ],
                default="Search by Name (Fuzzy)",
            ).execute()

            if action == "Exit":
                break

            media_list = []

            if action == "Search by Name (Fuzzy)":
                query = inquirer.text(message="Enter movie/series name:").execute()
                if not query:
                    continue
                media_list = self.fuzzy_search_media(query, media_type)
                
            elif action == "Filter by Genres & Countries":
                all_genres = self.get_genres()
                all_countries = self.get_countries()
                
                selected_genres = inquirer.checkbox(
                    message="Select Genres (Space to select, Enter to confirm):",
                    choices=all_genres,
                    instruction="(Leave empty to ignore genre filter)"
                ).execute()
                
                selected_countries = inquirer.checkbox(
                    message="Select Countries:",
                    choices=all_countries,
                    instruction="(Leave empty to ignore country filter)"
                ).execute()
                
                media_list = self.filter_media(genres=selected_genres, countries=selected_countries, media_type=media_type)

            elif action == "Search by Actor":
                actor_query = inquirer.text(message="Enter actor name:").execute()
                if actor_query:
                    media_list = self.filter_media(actors=[actor_query], media_type=media_type)

            elif action == "Search by Director":
                director_query = inquirer.text(message="Enter director name:").execute()
                if director_query:
                    media_list = self.filter_media(directors=[director_query], media_type=media_type)

            elif action == "Advanced Filters (Year, Rate, Time, Language)":
                all_languages = self.get_languages()
                
                selected_languages = inquirer.checkbox(
                    message="Select Languages:",
                    choices=all_languages,
                    instruction="(Leave empty to ignore language)"
                ).execute()

                min_rating = inquirer.number(
                    message="Minimum IMDB rating (0-10):",
                    default=0,
                    min_allowed=0,
                    max_allowed=10
                ).execute()

                min_year = inquirer.number(
                    message="Minimum Year (e.g., 2000):",
                    default=0,
                    min_allowed=0
                ).execute()

                max_year = inquirer.number(
                    message="Maximum Year (e.g., 2023):",
                    default=9999,
                    min_allowed=0
                ).execute()

                time_filter = inquirer.select(
                    message="Filter by Duration:",
                    choices=["Any", "Under 30 mins", "30 - 60 mins", "Over 60 mins"],
                    default="Any"
                ).execute()

                time_filter = None if time_filter == "Any" else time_filter

                media_list = self.filter_media(
                    languages=selected_languages, 
                    min_rating=float(min_rating), 
                    min_year=int(min_year), 
                    max_year=int(max_year), 
                    time_filter=time_filter, 
                    media_type=media_type
                )

            if not media_list:
                console.print("[yellow]No results found matching your criteria.[/yellow]")
                continue

            self.display_media_table(media_list)
            
            media_choices = [Choice(m['id'], f"{m['title']} ({m['type']})") for m in media_list]
            media_choices.append(Choice(None, "Go Back"))
            
            selected_media_id = inquirer.select(
                message="Select a title to see download links:",
                choices=media_choices,
            ).execute()

            if selected_media_id is None:
                continue

            links = self.get_links(selected_media_id)
            if not links:
                console.print("[yellow]No download links found for this title.[/yellow]")
                continue

            link_choices = []
            for link in links:
                _, season, episode, quality, sub_status, url = link
                if season and season > 0:
                    label = f"S{season:02d}E{episode} | {quality} | {sub_status}"
                else:
                    label = f"MOVIE | {quality} | {sub_status}"
                link_choices.append(Choice(link, label))
                
            link_choices.append(Choice(None, "Go Back"))

            selected_links = inquirer.checkbox(
                message="Select links to download (Space to select, Enter to confirm):",
                choices=link_choices,
            ).execute()

            if not selected_links:
                continue

            dest_dir = inquirer.text(
                message="Download directory:",
                default=self.default_download_dir,
            ).execute()
            
            os.makedirs(dest_dir, exist_ok=True)

            for link in selected_links:
                url = link[5]
                self.download_file(url, dest_dir)

        console.print("[bold blue]Goodbye![/bold blue]")

if __name__ == "__main__":
    cli = MediaCLI()
    cli.main_loop()