# Use the official Python image from Docker Hub
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container
COPY . /app

# Copy additional necessary files or folders into the container
# (Adjust the path to where your 'MwCameraControl_class.py' is located)
COPY "C:/Users/Admin/Desktop/MSIL Backup/DS Backup/mvs-test/GrabImage" /app/MvCameraControl_class

# Install dependencies required for the application
RUN apt-get update && \
    apt-get install -y libgl1 libglib2.0-0 && \
    pip install --no-cache-dir -r requirements.txt

# Set an environment variable to prevent Python from buffering stdout and stderr
ENV PYTHONUNBUFFERED=1

# Expose port 5000 for the container
EXPOSE 5000

# Specify the command to run the application (adjust to your entry file)
CMD ["python", "combined3.py"]