# Jellyfin
Python scripts and extensions to handle music metadata

Jellyfin is an opensource media player.  It has many features but managing the metadata for music can be frustrating. The supported tags are Artist, Album Artist and Genre.  Composers become "People" and are not linked to album tracks they wrote.
Jellyfin does not support multiple composers.

# Notes
Set up your custom delimiters in the Dashboard/Library/Libraries/<Music Library name> properties.  I set mine to ; as a delimiter and whitelisted & so Bob & Joe stays that way as an artist name
Multiple Album Artists use ;  or /as a delimiter
Tags are Case Sensitive, use a consistent naming convention such as Mixed Case for all Tags, Artist Names, Album Names
Track and Video names: The following characters are known to cause issues: <, >, :, ", /, \, |, ?, *

# Tools
Mp3Tag
Picard
