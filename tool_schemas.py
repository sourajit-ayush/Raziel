"""
tool_schemas.py - what the LLM sees of Raziel's tools: one JSON schema per tool, in the order the model
reads them. tools.py adds the schemas of the tools that live in their own modules and pairs each name
with its function (TOOL_SCHEMAS, TOOL_FUNCTIONS). Pure data: no imports, nothing runs.

Keep the order and wording stable: Ollama caches the start of every request (system prompt + tools), and
any change here makes it read all of it again once (10+ s on this PC).
"""

BASE_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Open an application on the user's computer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Name of the application to open, e.g. 'notepad', 'chrome', 'spotify'.",
                    }
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_website_search",
            "description": "Open a website with a search already performed, e.g. 'open Google and search for X' or 'open YouTube and search for Y'. Use this instead of open_app whenever the user wants to search on a specific site, not just open its homepage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "site": {
                        "type": "string",
                        "description": "The website to search on, e.g. 'google', 'youtube', 'wikipedia'.",
                    },
                    "query": {
                        "type": "string",
                        "description": "What to search for.",
                    },
                    "play": {
                        "type": "boolean",
                        "description": "True when the user said 'play X' (start playing the first result immediately), "
                                       "False/omitted when they said 'search for X' or 'look up X' (just show results). "
                                       "Only makes a difference on YouTube.",
                    },
                },
                "required": ["site", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_folder",
            "description": "Open a specific folder in File Explorer, such as Downloads, Documents, Desktop, or any named folder/path. Use this instead of open_app when the user wants to browse to a particular folder, not just open File Explorer in general.",
            "parameters": {
                "type": "object",
                "properties": {
                    "folder_name": {
                        "type": "string",
                        "description": "Name of the folder to open, e.g. 'downloads', 'desktop', or a full path.",
                    }
                },
                "required": ["folder_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_file",
            "description": "Open a specific file, document, PDF, or image by name or path. Searches common folders (Desktop, Downloads, Documents, Pictures, Videos, Music) if a full path isn't given.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Name or path of the file to open, e.g. 'resume.pdf' or 'vacation photo.jpg'.",
                    }
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_my_files",
            "description": "Search the user's own files (Documents, Downloads, Desktop) by meaning, offline, e.g. 'the PDF about the bank loan' or 'anything about my car insurance'. Returns the best matching files with a short excerpt each. Use it when the user asks what is in their files or where a document is and doesn't know its name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What the file is about, e.g. 'bank loan agreement' or 'python experience resume'.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a text file with the given content and save it locally.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Name of the file to create, e.g. 'notes.txt' or 'todo.md'.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The text content to put in the file.",
                    },
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search Google for current or uncertain facts: scores, prices, who holds an office, recent events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_datetime",
            "description": "Get the current real-world date and time. Use this whenever the user asks what day, date, or time it is.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save a fact to remember permanently, across future sessions and restarts. Use when the user says 'remember that...' or tells you something to keep in mind for later.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "The fact to remember, e.g. 'User has an exam on Friday' or 'User's favorite color is blue'.",
                    }
                },
                "required": ["fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "Search remembered facts and past conversations for something. Use when the user asks what you remember about something, or references a past conversation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for, e.g. 'exam' or 'what we talked about yesterday'.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget",
            "description": "Remove a remembered fact matching a keyword. Use when the user asks you to forget something.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Keyword to match against remembered facts for removal.",
                    }
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "Capture a screenshot of the user's current screen and save it as an image file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Optional name for the screenshot file, e.g. 'homework.png'. If not given, a timestamped name is used automatically.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_last_screenshot",
            "description": "Open the most recently taken screenshot so the user can see it.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_whatsapp_message",
            "description": "Send a WhatsApp message to a named contact. The contact's phone number must already be saved in CONTACTS. The user is asked to confirm out loud before it is actually sent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_name": {
                        "type": "string",
                        "description": "Name of the person to message, e.g. 'Fazal'.",
                    },
                    "message": {
                        "type": "string",
                        "description": "The message text to send.",
                    },
                },
                "required": ["contact_name", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compose_email",
            "description": "Write an email and open it as a pre-filled DRAFT in the user's mail. It never sends anything - the user reviews it and presses Send themselves. Write the full body in the user's voice.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient: a saved contact name or an email address. Empty if the user didn't say.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Short subject line.",
                    },
                    "body": {
                        "type": "string",
                        "description": "The complete email text, including a greeting and sign-off.",
                    },
                },
                "required": ["subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_music",
            "description": "Play a song or artist on Spotify RIGHT NOW, replacing whatever is playing. If the user just says 'play some music' without specifying what, call this with an empty query to play a popular playlist. Do NOT use this when the user says 'queue' or 'add to the queue' - use queue_music for that.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Song name and/or artist to play, e.g. 'Blinding Lights The Weeknd'. Leave empty for generic/random music.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_playlist",
            "description": "Play one of the user's own saved Spotify playlists by name, e.g. 'Midnight Melodies' or 'Liked Songs'. Use this instead of play_music when the user names a specific playlist rather than a song or artist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "playlist_name": {
                        "type": "string",
                        "description": "Name of the playlist to play, e.g. 'Midnight Melodies'.",
                    }
                },
                "required": ["playlist_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pause_music",
            "description": "Pause Spotify playback.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resume_music",
            "description": "Resume Spotify playback from where it was paused.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "next_track",
            "description": "Skip to the next track on Spotify.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "previous_track",
            "description": "Go back to the previous track on Spotify.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "queue_music",
            "description": "Add a song to the END of the user's Spotify queue WITHOUT interrupting what is playing now. Use whenever the user says 'queue', 'add X to the queue' or 'add X to my queue'. Never use play_music for those.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Song name and/or artist to queue, e.g. 'Kesariya Arijit Singh'.",
                    }
                },
                "required": ["query"],
            },
        },
    },
]
