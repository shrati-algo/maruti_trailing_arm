import os
import zipfile

# Root folder
SOURCE_DIR = r"C:\Users\USER\Documents\frame_grab"
ZIP_FILE = r"frame_grab_filtered.zip"

# Folders to exclude
EXCLUDE_FOLDERS = {
    "images",
    "best_images_cam1",
    "image_data",
    "total_images"
}

with zipfile.ZipFile(ZIP_FILE, 'w', zipfile.ZIP_DEFLATED) as zipf:
    for root, dirs, files in os.walk(SOURCE_DIR):
        
        # Remove excluded folders from traversal
        dirs[:] = [d for d in dirs if d not in EXCLUDE_FOLDERS]
        
        for file in files:
            file_path = os.path.join(root, file)
            
            # Add file to zip
            arcname = os.path.relpath(file_path, SOURCE_DIR)
            zipf.write(file_path, arcname)

print(f"ZIP created successfully: {ZIP_FILE}")